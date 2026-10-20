"""Iterative <=3-scattering transport with actual area/BSDF MIS and shared RR."""
from dataclasses import dataclass, field
import math

from .multibounce_policy import validate_workload, RR_POLICY
from .bsdf import evaluate_bsdf, evaluate_pdf, active_lobes, alpha_for_material
from .bsdf_sampling import sample_bsdf_addressed, BsdfSample
from .sampling import uniform_sample
from .pbr_numeric import vector
from .pbr_errors import InvalidBsdfPdf, InvalidBsdfSample
from .area_emitters import sample_emitter
from .mis import power_weight
from .roulette import roulette
from .transport_errors import InvalidTransport
from drt_backend.raytracing.tracing.surface_queries import trace_occluded

BLACK=(0.,0.,0.)


def rgb(value):
    return vector(value,3,InvalidTransport,minimum=0)


def multiply(*values):
    result=(1.,1.,1.)
    for value in values:
        value=rgb(value)
        result=rgb(tuple(a*b for a,b in zip(result,value)))
    return result


def sum_rgb(values):
    values=tuple(values)
    try: return rgb(tuple(math.fsum(v[c] for v in values) for c in range(3)))
    except (ValueError,OverflowError) as error: raise InvalidTransport('Path radiance sum overflow.') from error


def advance_throughput(beta,sample):
    if type(sample) is not BsdfSample: raise InvalidBsdfSample('Expected validated M6.1 BsdfSample.')
    sample.__post_init__()  # explicit measure/weight validation, no numerical repair
    return multiply(beta,sample.throughput_weight)


@dataclass(frozen=True,slots=True)
class PreviousVertex:
    position: tuple
    bsdf_density: float
    event: str
    area_strategy: bool


@dataclass(slots=True)
class PathState:
    """One invocation owns all mutation. No state is shared between addresses."""
    pixel_id: int
    sample_id: int
    ray: object
    guard: object = None
    scatter_count: int = 0
    beta: tuple = (1.,1.,1.)
    previous: PreviousVertex | None = None
    contributions: list = field(default_factory=list)


@dataclass(frozen=True,slots=True)
class VertexTraceV1:
    bounce: int
    identity: tuple
    position: tuple
    throughput_in: tuple
    emission_weight: float
    emission_term: tuple
    roulette: object
    point_term: tuple
    area_term: tuple
    area_pdf: float
    competing_bsdf_pdf: float
    area_weight: float
    area_visible: bool | None
    event: str
    outgoing_pdf: float
    throughput_out: tuple


@dataclass(frozen=True,slots=True)
class PathResultV1:
    radiance: tuple
    termination: str
    vertices: tuple
    schema_version: int = 1

    def __post_init__(self):
        object.__setattr__(self,'radiance',rgb(self.radiance))


def emitter_hit_weight(scene,previous,surface):
    if previous is None or previous.event=='delta' or not previous.area_strategy: return 1.0
    competing=scene.hit_light_pdf(previous.position,surface)
    return 1.0 if competing==0 else power_weight(previous.bsdf_density,competing)


def sample_path(scene,pixel_id,sample_id,effort,*,checkpoint=lambda:None):
    w=scene.workload;environment,_=validate_workload(w)
    normal_transport=getattr(scene,'transport_correction',None)
    if type(sample_id) is not int or not 0<=sample_id<w.target_spp: raise InvalidTransport('SampleId outside target SPP.')
    checkpoint()
    state=PathState(pixel_id,sample_id,scene.camera.addressed(w.seed,pixel_id,sample_id))
    effort.values['path_count']+=1
    vertices=[];termination='depth_limit'
    for _ in range(w.max_path_depth+1):
        checkpoint()
        surface=effort.query('primary_rays' if state.scatter_count==0 else 'secondary_rays',
                            lambda:scene.closest(state.ray,state.guard))
        if surface is not None: effort.values['surface_interactions']+=1
        checkpoint()
        if surface is None:
            state.contributions.append(multiply(state.beta,environment))  # no environment NEE
            termination='environment';break
        material,frame=surface.material,surface.frame
        p=tuple(float(c) for c in surface.accepted.surface.position_world)
        h=surface.accepted.hit;identity=(h.instance_index,h.primitive_index,h.triangle_index)
        incoming_beta=state.beta
        emission_weight=emitter_hit_weight(scene,state.previous,surface)
        emission=multiply(state.beta,material.emission,(emission_weight,)*3)
        state.contributions.append(emission)
        rr=None;point_term=area_term=BLACK;light_pdf=competing_pdf=area_weight=0.;area_visible=None
        event='depth_limit';outgoing_pdf=0.;next_beta=state.beta
        def record():
            vertices.append(VertexTraceV1(state.scatter_count,identity,p,incoming_beta,emission_weight,
                emission,rr,point_term,area_term,light_pdf,competing_pdf,area_weight,area_visible,
                event,outgoing_pdf,next_beta))
        if state.scatter_count==w.max_path_depth:
            record();break
        if state.scatter_count==2 and not any(material.emission) and w.termination_policy==RR_POLICY:
            rr=roulette(state.beta,seed=w.seed,pixel_id=pixel_id,sample_id=sample_id,bounce=2)
            if not rr.survived:
                event=termination='rr_terminated';next_beta=BLACK;record();break
            state.beta=rr.throughput
        wo_world=tuple(-float(c) for c in state.ray.direction);wo=frame.to_local(wo_world)
        points=[]
        for light in scene.point_samples(surface):
            checkpoint()
            wi_world=tuple(float(c) for c in light.incoming_direction)
            if not frame.valid_reflection(wi_world,wo_world): continue
            wi=frame.to_local(wi_world);value=evaluate_bsdf(material,wi,wo,policy=w.bsdf).value
            if not any(value): continue
            spawned=scene.spawn_shadow(state.ray,surface,light)
            blocked=effort.query('shadow_rays',lambda:trace_occluded(scene.prepared.textures,spawned.ray,spawned.guard))
            checkpoint()
            if not blocked:
                factor=1.0 if normal_transport is None else normal_transport(frame,wi_world,wo_world)
                points.append(multiply(state.beta,value,tuple(factor*wi[2]*float(c) for c in light.incident_rgb)))
        point_term=sum_rgb(points);state.contributions.append(point_term)
        diffuse,specular=active_lobes(material)
        continuous=diffuse or specular and alpha_for_material(material,policy=w.bsdf)>0
        area_strategy=bool(scene.emitters) and continuous
        if area_strategy:
            base=16+16*state.scatter_count
            uniforms=tuple(uniform_sample(w.seed,pixel_id,sample_id,base+i) for i in (3,4,5))
            selected=sample_emitter(scene.emitters,p,uniforms)
            if selected is not None and frame.valid_reflection(selected.direction,wo_world):
                wi=frame.to_local(selected.direction)
                value=evaluate_bsdf(material,wi,wo,policy=w.bsdf).value
                light_pdf=selected.solid_angle_pdf
                competing_pdf=evaluate_pdf(material,wi,wo,policy=w.bsdf).solid_angle_density
                area_weight=power_weight(light_pdf,competing_pdf)
                if any(value):
                    segment=scene.spawn_area(state.ray,surface,selected)
                    blocked=effort.query('shadow_rays',lambda:scene.area_occluded(segment))
                    checkpoint();area_visible=not blocked
                    if not blocked:
                        area_term=multiply(state.beta,value,selected.emitter.emission,
                                           (wi[2]*area_weight/light_pdf,)*3)
            state.contributions.append(area_term)
        checkpoint()
        sample=sample_bsdf_addressed(material,wo,seed=w.seed,pixel_id=pixel_id,sample_id=sample_id,
                                     bounce=state.scatter_count,policy=w.bsdf)
        event=sample.event
        next_beta=advance_throughput(state.beta,sample)
        if sample.event=='continuous':
            expected=evaluate_pdf(material,sample.wi,wo,policy=w.bsdf)
            if sample.pdf!=expected: raise InvalidBsdfPdf('Sample does not use the actual full mixture density.')
            outgoing_pdf=sample.pdf.solid_angle_density
        if not sample.can_contribute or not any(next_beta):
            termination='null' if sample.event=='null' else 'zero_throughput';record();break
        direction=frame.to_world(sample.wi)
        if not frame.valid_reflection(direction,wo_world):
            if normal_transport is None:raise InvalidTransport('Outgoing path leaves geometric reflection hemisphere.')
            event=termination='normal_hemisphere_null';next_beta=BLACK;record();break
        if normal_transport is not None:
            factor=normal_transport(frame,direction,wo_world)
            next_beta=multiply(next_beta,(factor,)*3)
            if not any(next_beta):
                event=termination='normal_zero_throughput';record();break
        spawned=scene.spawn_continuation(state.ray,surface,direction)
        record()
        state.previous=PreviousVertex(p,outgoing_pdf,event,area_strategy)
        state.ray,state.guard=spawned.ray,spawned.guard
        state.beta=next_beta;state.scatter_count+=1
    return PathResultV1(sum_rgb(state.contributions),termination,tuple(vertices))
