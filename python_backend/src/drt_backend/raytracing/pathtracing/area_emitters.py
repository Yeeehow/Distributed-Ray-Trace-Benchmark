"""Uniform selection of <=2 source-owned triangles and area/solid-angle measures."""
from dataclasses import dataclass
import math
from .pbr_numeric import vector, scalar, dot, cross, normalize
from .transport_errors import InvalidTransport


class InvalidEmitter(InvalidTransport):
    """Invalid emitter identity, geometry, orientation or probability density."""


def triangle_geometry(vertices):
    if type(vertices) is not tuple or len(vertices)!=3: raise InvalidEmitter('Three world vertices required.')
    v=tuple(vector(p,3,InvalidEmitter) for p in vertices)
    edges=tuple(tuple(v[k][j]-v[0][j] for j in range(3)) for k in (1,2))
    normal=cross(*edges)
    area=math.hypot(*normal)/2
    if not 0<area<math.inf: raise InvalidEmitter('World triangle area must be finite and positive.')
    return v,area,normalize(normal,InvalidEmitter)


@dataclass(frozen=True,slots=True)
class AreaEmitter:
    identity: tuple
    vertices: tuple
    normal: tuple
    emission: tuple
    double_sided: bool
    instance: object
    primitive: object
    area: float = 0.0

    def __post_init__(self):
        if (type(self.identity) is not tuple or len(self.identity)!=3
                or any(type(i) is not int or i<0 for i in self.identity) or type(self.double_sided) is not bool):
            raise InvalidEmitter('Invalid emitter identity/sidedness.')
        vertices,area,winding=triangle_geometry(self.vertices)
        normal=vector(self.normal,3,InvalidEmitter,unit=True)
        if abs(abs(dot(normal,winding))-1)>2e-12: raise InvalidEmitter('Emitter normal differs from its world plane.')
        emission=vector(self.emission,3,InvalidEmitter,minimum=0)
        if not any(emission): raise InvalidEmitter('Admitted emitter must emit nonzero radiance.')
        for name,value in dict(vertices=vertices,area=area,normal=normal,emission=emission).items():
            object.__setattr__(self,name,value)

    def solid_angle_pdf(self, origin, target, selection_probability):
        origin=vector(origin,3,InvalidEmitter);target=vector(target,3,InvalidEmitter)
        probability=scalar(selection_probability,InvalidEmitter,minimum=0,maximum=1)
        if probability<=0: raise InvalidEmitter('Emitter selection probability must be positive.')
        delta=tuple(b-a for a,b in zip(origin,target));distance=math.hypot(*delta)
        if not 0<distance<math.inf: raise InvalidEmitter('Emitter distance must be finite and positive.')
        wi=tuple(c/distance for c in delta)
        cosine=dot(self.normal,tuple(-c for c in wi))
        if self.double_sided: cosine=abs(cosine)
        if cosine<=0: return 0.0  # supported geometric back face/tangent, not a repaired PDF
        value=(probability/self.area)*distance*distance/cosine
        if not 0<value<math.inf: raise InvalidEmitter('Area-to-solid-angle density is unrepresentable.')
        return value


@dataclass(frozen=True,slots=True)
class AreaSample:
    emitter: AreaEmitter
    position: tuple
    barycentric: tuple
    direction: tuple
    distance: float
    area_pdf: float
    solid_angle_pdf: float


def sample_emitter(emitters, origin, uniforms):
    if type(emitters) is not tuple or not 1<=len(emitters)<=2 or any(type(e) is not AreaEmitter for e in emitters):
        raise InvalidEmitter('Sampling needs one or two explicit AreaEmitters.')
    u=vector(uniforms,3,InvalidEmitter,minimum=0,maximum=math.nextafter(1.0,0.0))
    origin=vector(origin,3,InvalidEmitter)
    emitter=emitters[math.floor(u[0]*len(emitters))]
    root=math.sqrt(u[1]);bary=(1-root,root*(1-u[2]),root*u[2])
    position=tuple(math.fsum(bary[k]*emitter.vertices[k][j] for k in range(3)) for j in range(3))
    density=emitter.solid_angle_pdf(origin,position,1/len(emitters))
    if density==0: return None
    delta=tuple(b-a for a,b in zip(origin,position));distance=math.hypot(*delta)
    area_pdf=(1/len(emitters))/emitter.area
    if not 0<area_pdf<math.inf: raise InvalidEmitter('Invalid area density.')
    return AreaSample(emitter,position,bary,tuple(c/distance for c in delta),distance,area_pdf,density)
