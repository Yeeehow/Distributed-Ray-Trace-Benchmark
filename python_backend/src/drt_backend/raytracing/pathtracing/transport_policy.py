"""Exact executable policy tuples for the bounded synthetic Serial workload."""
from .contracts import PolicyDescriptor, WorkloadSpec, WORKLOAD_KIND
from .sampling import SAMPLER_VERSION, DIMENSION_LAYOUT_VERSION
from .pbr_material import MATERIAL_POLICY, validate_material_policy
from .bsdf import BSDF_POLICY, validate_bsdf_policy
from .pbr_numeric import vector
from .transport_errors import UnsupportedTransport, InvalidTransport


def policy(identity, **parameters):
    return PolicyDescriptor(identity, tuple(sorted(parameters.items())))


INTEGRATOR_POLICY = policy('drt.integrator.serial_first_hit_mc.v1',
    depth=1, camera='addressed_xy_0_1_top_left_perspective',
    surface='owned_opaque_ns_equals_oriented_ng_2e-12',
    continuation='full_mixture_continuous_discrete_delta_null_no_resample',
    emission='primary_and_secondary_endpoint_only_unit_radiance',
    environment='bsdf_only_constant', punctual='all_point_delta_position_no_mis',
    origin='existing_exact_contact_certification', summation='ascending_sample_math_fsum_float64',
    errors='typed_fatal_no_black_repair')
TERMINATION_POLICY = policy('drt.termination.first_scattering.v1', secondary='emission_or_environment_no_scatter')
ALPHA_POLICY = policy('drt.alpha.opaque_only.v1', unsupported='mask_blend_fail_before_rays')
RENDERING_POLICY = policy('drt.rendering.synthetic_serial.v1',
    dimensions='4x3_or_7x5', maximum_spp=8, resource='raw_glb_sha256_and_ecs_fingerprint',
    surfaces='source_identity_then_m61_resolution')
COLOR_POLICY = policy('drt.color.linear_srgb_clamp_ev0.v1',
    exposure_ev=0, order='validate_clamp_piecewise_srgb_quantize', rounding='floor_255x_plus_half')
ORIGINAL_SHA256 = '553b9bd35d9234a319b2d8424fb8c0e95a0af722327ddd6103485d7d809ff13b'


def lighting_policy(environment):
    rgb = vector(environment, 3, InvalidTransport, minimum=0)
    return policy('drt.lighting.synthetic_radiometric.v1',
        environment_r=rgb[0], environment_g=rgb[1], environment_b=rgb[2],
        units='emission_radiance_point_color_times_intensity_radiant_per_sr',
        point='metadata_point_no_range_inverse_square', maximum_point_lights=1)


def validate_workload(workload):
    if type(workload) is not WorkloadSpec:
        raise InvalidTransport('Expected immutable WorkloadSpec.')
    if ((workload.width, workload.height) not in ((4, 3), (7, 5))
            or not 1 <= workload.target_spp <= 8 or workload.max_path_depth != 1):
        raise UnsupportedTransport('M6.2 supports only 4x3/7x5, SPP<=8 and scattering depth 1.')
    if workload.scene_sha256 == ORIGINAL_SHA256:
        raise UnsupportedTransport('Original scene execution is outside M6.2 admission.')
    validate_material_policy(workload.material_model)
    validate_bsdf_policy(workload.bsdf)
    for name, expected in (('integrator', INTEGRATOR_POLICY), ('termination_policy', TERMINATION_POLICY),
                           ('alpha_policy', ALPHA_POLICY), ('rendering_policy', RENDERING_POLICY),
                           ('color_output_policy', COLOR_POLICY)):
        if getattr(workload, name) != expected:
            raise UnsupportedTransport('Unsupported executable policy: ' + name)
    parameters = dict(workload.lighting_policy.parameters)
    try:
        env = tuple(parameters['environment_'+c] for c in ('r', 'g', 'b'))
    except KeyError as error:
        raise UnsupportedTransport('Missing explicit constant environment.') from error
    if workload.lighting_policy != lighting_policy(env):
        raise UnsupportedTransport('Unsupported lighting identity or parameters.')
    return vector(env, 3, InvalidTransport, minimum=0)


def make_workload(scene_sha256, *, width=4, height=3, scene_index=0, camera_node_index=0,
                  seed=19, target_spp=8, environment=(0.0, 0.0, 0.0)):
    workload = WorkloadSpec(WORKLOAD_KIND, 1, scene_sha256, scene_index, camera_node_index,
        width, height, SAMPLER_VERSION, DIMENSION_LAYOUT_VERSION, seed, target_spp, 8, 1,
        TERMINATION_POLICY, MATERIAL_POLICY, BSDF_POLICY, INTEGRATOR_POLICY, ALPHA_POLICY,
        lighting_policy(environment), RENDERING_POLICY, COLOR_POLICY)
    validate_workload(workload)
    return workload
