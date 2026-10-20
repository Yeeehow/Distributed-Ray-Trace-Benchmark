"""Additive multi-bounce scene and certified finite-emitter visibility bridge.

Source departure uses the unchanged exact Legacy core. Target support is checked
against its exact affine plane and existing kernel candidates in a bounded end
band. Unresolved distinct endpoint geometry fails; no emitter/occluder epsilon.
"""
from dataclasses import dataclass, field
from fractions import Fraction as F
import math
import numpy as np

from .scene_adapter import PbrSource, PbrSceneAdapter
from .multibounce_policy import validate_workload, resource_view
from .area_emitters import AreaEmitter, InvalidEmitter
from .pbr_material import resolve_material, ResolvedTextureSamples
from .pbr_numeric import normalize, vector
from .transport_errors import InvalidTransport
from drt_backend.raytracing.tracing.surface_queries import (_shadow_geometric_candidates,
    _iter_geometric_candidates,_evaluate_camera_candidate,ShadowNumericalError)
from drt_backend.raytracing.shading.texture_sampling import SamplingPolicy
from drt_backend.raytracing.shading.shadow_visibility import _spawn, _rounded, _dot, _sub, _cross
from drt_backend.raytracing.model.rays import Ray


def load_source(path, workload):
    return PbrSource.load(path,resource_view(workload))


def _exact_vertices(instance, primitive, triangle):
    matrix=instance.world_matrix
    local=primitive.positions[primitive.triangle_indices[triangle]]
    return tuple(tuple(sum((F(float(matrix[j,k]))*F(float(p[k])) for k in range(3)),F(0))+
        F(float(matrix[j,3])) for j in range(3)) for p in local)


@dataclass(frozen=True,slots=True)
class _Endpoint:
    position: tuple


@dataclass(frozen=True,slots=True)
class _FiniteDirection:
    incoming_direction: tuple
    light: _Endpoint


@dataclass(frozen=True,slots=True)
class CertifiedAreaSegment:
    spawned: object
    emitter: AreaEmitter
    exact_vertices: tuple
    exact_normal: tuple
    exact_target_t: F
    support_end: float


@dataclass(frozen=True,slots=True,eq=False)
class MultiScene(PbrSceneAdapter):
    emitters: tuple = field(default=())

    @classmethod
    def prepare(cls,source,workload,*,checkpoint=lambda:None):
        _,identities=validate_workload(workload)
        base=PbrSceneAdapter.prepare(source,resource_view(workload),checkpoint=checkpoint)
        try:
            emitters=[]
            for identity in identities:
                i,p,t=identity
                if i>=len(base.prepared.scene.mesh_instances): raise InvalidEmitter('Emitter instance out of range.')
                instance=base.prepared.scene.mesh_instances[i]
                if p>=len(instance.definition.primitives): raise InvalidEmitter('Emitter primitive out of range.')
                primitive=instance.definition.primitives[p]
                base.prepared.textures.shaded.require_primitive(primitive)
                if t>=len(primitive.triangle_indices): raise InvalidEmitter('Emitter triangle out of range.')
                local=primitive.positions[primitive.triangle_indices[t]]
                with np.errstate(over='raise',invalid='raise'):
                    vertices=local@instance.world_matrix[:3,:3].T+instance.world_matrix[:3,3]
                    local_normal=np.cross(local[1]-local[0],local[2]-local[0])
                    normal=base.prepared.acceleration.prepared_instances[i].inverse_linear.T@local_normal
                material=base.prepared.metadata.material_for_primitive(primitive)
                resolved=resolve_material(material,ResolvedTextureSamples())
                emitters.append(AreaEmitter(identity,tuple(tuple(float(c) for c in v) for v in vertices),
                    normalize(normal,InvalidEmitter),resolved.emission,resolved.double_sided,instance,primitive))
                checkpoint()
            return cls(source,workload,base.prepared,base.camera,tuple(emitters))
        except BaseException:
            base.close();raise

    def emitter_for(self,surface):
        h=surface.accepted.hit
        key=(h.instance_index,h.primitive_index,h.triangle_index)
        for emitter in self.emitters:
            if key==emitter.identity:
                if (surface.accepted.surface.instance is not emitter.instance
                        or surface.accepted.surface.primitive is not emitter.primitive):
                    raise InvalidEmitter('Hit emitter has foreign source geometry.')
                return emitter
        return None

    def hit_light_pdf(self,origin,surface):
        emitter=self.emitter_for(surface)
        return 0.0 if emitter is None else emitter.solid_angle_pdf(origin,surface.accepted.surface.position_world,1/len(self.emitters))

    def spawn_area(self,source_ray,surface,sample):
        if not any(sample.emitter is e for e in self.emitters): raise InvalidEmitter('Area sample has foreign emitter ownership.')
        instance,primitive,inverse=self._owned(source_ray,surface.accepted)
        direction=vector(sample.direction,3,InvalidEmitter,unit=True)
        spawned=_spawn(self.prepared.textures.shaded,source_ray,surface.accepted,
            _FiniteDirection(direction,_Endpoint(sample.position)),instance,primitive,inverse)
        emitter=sample.emitter
        vertices=_exact_vertices(emitter.instance,emitter.primitive,emitter.identity[2])
        normal=_cross(_sub(vertices[1],vertices[0]),_sub(vertices[2],vertices[0]))
        origin=tuple(F(float(c)) for c in spawned.ray.origin)
        outgoing=tuple(F(float(c)) for c in spawned.ray.direction)
        divisor=_dot(normal,outgoing)
        if not any(normal) or not divisor: raise ShadowNumericalError('Degenerate/tangent target plane.')
        distance=_dot(normal,_sub(vertices[0],origin))/divisor
        if distance<=F(spawned.ray.t_max): raise ShadowNumericalError('Finite visibility endpoint does not precede the exact emitter plane.')
        point=tuple(a+distance*b for a,b in zip(origin,outgoing))
        axis=max(range(3),key=lambda j:abs(normal[j]));a,b=[j for j in range(3) if j!=axis]
        e1,e2,offset=_sub(vertices[1],vertices[0]),_sub(vertices[2],vertices[0]),_sub(point,vertices[0])
        det=e1[a]*e2[b]-e1[b]*e2[a]
        u=(offset[a]*e2[b]-offset[b]*e2[a])/det
        v=(e1[a]*offset[b]-e1[b]*offset[a])/det
        if u<=0 or v<=0 or u+v>=1:
            raise ShadowNumericalError('Rounded segment lacks a strict interior target certificate.')
        end=math.nextafter(_rounded(distance,True),math.inf)
        return CertifiedAreaSegment(spawned,emitter,vertices,normal,distance,end)

    def area_occluded(self,segment):
        """One logical visibility ray, including source and target support probes."""
        texture=self.prepared.textures;ray=segment.spawned.ray
        for hit in _shadow_geometric_candidates(texture,ray,segment.spawned.guard,max_candidates=65536):
            if (hit.instance_index,hit.primitive_index,hit.triangle_index)==segment.emitter.identity:
                raise ShadowNumericalError('Kernel target enters certified open visibility interval.')
            if _evaluate_camera_candidate(texture,ray,hit,SamplingPolicy()) is not None: return True
        end_ray=Ray(ray.origin,ray.direction,ray.t_max,segment.support_end)
        for hit in _iter_geometric_candidates(self.prepared.acceleration,end_ray,max_candidates=65536):
            instance,primitive,_=texture.shaded.resolve_hit(hit)
            vertices=_exact_vertices(instance,primitive,hit.triangle_index)
            if any(_dot(segment.exact_normal,_sub(v,segment.exact_vertices[0])) for v in vertices):
                raise ShadowNumericalError('Distinct geometry in the unresolved target endpoint band.')
            # Exact common target plane, approached from strictly before it:
            # no point of this candidate plane lies in the open segment.
        return False
