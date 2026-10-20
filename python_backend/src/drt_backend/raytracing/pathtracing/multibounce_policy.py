"""Independent bounded multi-bounce policy; the accepted first-hit policy is unchanged."""
import json
from .contracts import WorkloadSpec, PolicyDescriptor, WORKLOAD_KIND
from .sampling import SAMPLER_VERSION, DIMENSION_LAYOUT_VERSION
from .pbr_material import MATERIAL_POLICY, validate_material_policy
from .bsdf import BSDF_POLICY, validate_bsdf_policy
from .transport_policy import (policy, ALPHA_POLICY, COLOR_POLICY, ORIGINAL_SHA256,
                               make_workload as first_hit_workload)
from .pbr_numeric import vector
from .transport_errors import InvalidTransport, UnsupportedTransport

MIS_POLICY = policy('drt.mis.power2_one_light_one_bsdf.v1', exponent=2, light_samples=1, bsdf_samples=1,
    measure='conditional_solid_angle_full_selection_and_mixture', delta='not_compared')
RR_POLICY = policy('drt.termination.rr_after_two_scattering_vertices.v1', activation_bounce=2,
    applies_to='nonemissive_nonterminal_vertices', maximum_depth=3, q_min=0.10, q_max=0.95,
    probability='clamped_max_beta', placement='before_both_nee_and_bsdf_after_emission', compensation='beta_over_q')
DEPTH_POLICY = policy('drt.termination.bounded_depth_only.v1', maximum_depth=3, roulette='disabled')
INTEGRATOR_POLICY = policy('drt.integrator.serial_multibounce_nee_mis_rr.v1',
    camera='m62_addressed_xy_0_1', depth='one_to_three_scattering_events',
    emission='once_per_vertex_previous_technique_mis', environment='bsdf_only_weight_one',
    surface='owned_opaque_ns_equals_oriented_ng_2e-12', origin='exact_source_and_finite_target_contact',
    continuation='m61_full_mixture_delta_null_no_resample',
    mis=MIS_POLICY.identity, summation='ordered_path_terms_ascending_samples_math_fsum_float64', errors='typed_fatal')
RENDERING_POLICY = policy('drt.rendering.synthetic_multibounce_serial.v1', dimensions='4x3_or_7x5', maximum_spp=8,
    resource='raw_glb_and_ecs_read_only', result='pbr_radiance_block_v1', diagnostics='executed_path_trace_v1')


def encode_emitters(identities):
    if (type(identities) is not tuple or len(identities)>2 or any(type(k) is not tuple or len(k)!=3
            or any(type(i) is not int or i<0 for i in k) for k in identities)
            or identities != tuple(sorted(set(identities)))):
        raise InvalidTransport('Emitter IDs require at most two sorted unique (instance,primitive,triangle) tuples.')
    return ';'.join(':'.join(map(str,k)) for k in identities) if identities else 'none'


def decode_emitters(encoded):
    try:
        result=() if encoded=='none' else tuple(tuple(int(v) for v in k.split(':')) for k in encoded.split(';'))
        if encode_emitters(result)!=encoded: raise ValueError('Noncanonical IDs')
        return result
    except (TypeError,ValueError,AttributeError) as error:
        raise InvalidTransport('Invalid explicit emitter identity list.') from error


def lighting_policy(environment, emitter_ids=()):
    rgb=vector(environment,3,InvalidTransport,minimum=0)
    return policy('drt.lighting.synthetic_points_triangles.v1', environment_r=rgb[0],environment_g=rgb[1],environment_b=rgb[2],
        point='m62_numeric_radiant_intensity_inverse_square', maximum_point_lights=1,
        emitters=encode_emitters(emitter_ids), selection='uniform_over_explicit_triangles',
        area='world_area_sqrt_barycentrics', emission='unit_resolved_radiance_material_sidedness',
        visibility='exact_source_departure_target_plane_and_endpoint_band', mis=MIS_POLICY.identity,
        light_samples=1, bsdf_samples=1)


def _same(actual, expected):
    return (type(actual) is PolicyDescriptor and json.dumps(actual.canonical_data(),sort_keys=True)==
            json.dumps(expected.canonical_data(),sort_keys=True))


def validate_workload(w):
    if getattr(getattr(w,'integrator',None),'identity',None)=='drt.integrator.room_multibounce_nee_mis_rr.v1':
        from drt_backend.room_pbr.policy import validate
        return validate(w)
    if type(w) is not WorkloadSpec: raise InvalidTransport('Expected immutable WorkloadSpec.')
    if ((w.width,w.height) not in ((4,3),(7,5)) or not 1<=w.target_spp<=8 or not 1<=w.max_path_depth<=3
            or w.scene_sha256==ORIGINAL_SHA256):
        raise UnsupportedTransport('Only bounded synthetic 4x3/7x5, <=8-SPP, depth1..3 are admitted.')
    validate_material_policy(w.material_model);validate_bsdf_policy(w.bsdf)
    for name,expected in (('integrator',INTEGRATOR_POLICY),('alpha_policy',ALPHA_POLICY),
                          ('rendering_policy',RENDERING_POLICY),('color_output_policy',COLOR_POLICY),
                          ('material_model',MATERIAL_POLICY),('bsdf',BSDF_POLICY)):
        if not _same(getattr(w,name),expected): raise UnsupportedTransport('Unsupported multi-bounce policy: '+name)
    if not any(_same(w.termination_policy,p) for p in (RR_POLICY,DEPTH_POLICY)):
        raise UnsupportedTransport('Unsupported termination policy.')
    p=dict(w.lighting_policy.parameters)
    try:
        env=tuple(p['environment_'+c] for c in ('r','g','b'));ids=decode_emitters(p['emitters'])
    except KeyError as error: raise UnsupportedTransport('Explicit lighting parameters required.') from error
    if not _same(w.lighting_policy,lighting_policy(env,ids)): raise UnsupportedTransport('Unsupported lighting policy.')
    return vector(env,3,InvalidTransport,minimum=0),ids


def make_workload(scene_sha256, *, width=4,height=3,scene_index=0,camera_node_index=0,seed=19,
                  target_spp=8,max_path_depth=3,environment=(0.,0.,0.),emitter_ids=(),rr=True):
    if type(rr) is not bool: raise InvalidTransport('RR enablement must be bool.')
    w=WorkloadSpec(WORKLOAD_KIND,1,scene_sha256,scene_index,camera_node_index,width,height,SAMPLER_VERSION,
        DIMENSION_LAYOUT_VERSION,seed,target_spp,8,max_path_depth,RR_POLICY if rr else DEPTH_POLICY,
        MATERIAL_POLICY,BSDF_POLICY,INTEGRATOR_POLICY,ALPHA_POLICY,lighting_policy(environment,emitter_ids),
        RENDERING_POLICY,COLOR_POLICY)
    validate_workload(w)
    return w


def resource_view(w):
    """Only resource preparation/preflight is borrowed; never execute the old estimator."""
    env,_=validate_workload(w)
    return first_hit_workload(w.scene_sha256,width=w.width,height=w.height,scene_index=w.scene_index,
        camera_node_index=w.camera_node_index,seed=w.seed,target_spp=w.target_spp,environment=env)
