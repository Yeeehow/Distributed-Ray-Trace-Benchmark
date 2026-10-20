"""Exact original-room source, camera, lighting and radiance transport policy."""
from dataclasses import replace
from drt_backend.raytracing.pathtracing.contracts import WorkloadSpec
from drt_backend.raytracing.pathtracing.multibounce_policy import (
    make_workload as tiny_workload, INTEGRATOR_POLICY, RR_POLICY, DEPTH_POLICY)
from drt_backend.raytracing.pathtracing.transport_policy import policy, ORIGINAL_SHA256
from drt_backend.raytracing.pathtracing.transport_errors import UnsupportedTransport

RAW=ORIGINAL_SHA256
NORMAL_POLICY=policy('drt.normal_transport.reciprocal_projected_area_budget.v1',
    measure='geometric_solid_angle_radiance',hemispheres='both_Ns_and_oriented_Ng',
    bsdf='f_s_times_min_1_Nsi_over_Ngi_times_min_1_Nso_over_Ngo',
    relative_weight='min_1_Ngi_over_Nsi_times_min_1_Nso_over_Ngo',
    invalid_draw='null_no_resample',contact='unchanged_exact_source_geometry')
LIGHTING_POLICY=policy('drt.lighting.gltf_punctual_room.v1',lights='three_point_two_spot_metadata_order',
    units='KHR_lights_punctual_candela_numeric_photometric_RGB',distance='inverse_square',
    cone='square_clamped_linear_cosine',selection='all_five_delta_no_continuous_mis',
    environment='black',emission='original_factor_times_strength_no_area_NEE')
ROOM_INTEGRATOR=policy('drt.integrator.room_multibounce_nee_mis_rr.v1',
    base=INTEGRATOR_POLICY.identity,normal_transport=NORMAL_POLICY.identity,
    local_bsdf='unchanged_M61_conservative',invalid_geometric_direction='null_no_resample')
RENDERING_POLICY=policy('drt.rendering.original_room_cpu.v1',dimensions='160x90_or_640x360',
    maximum_spp=8,max_depth=3,camera=5,sample_block_size=8,source='exact_original_sha256_ecs')
COLOR_POLICY=policy('drt.color.room_srgb_exposure.v1',exposure_ev=-7,
    order='validate_linear_scale_2powEV_clamp_piecewise_srgb_quantize',rounding='floor_255x_plus_half',
    calibration='visual_functional_preset_not_official_photometric_calibration')


def make_workload(*,width=160,height=90,spp=1,depth=1,seed=19):
    template=tiny_workload('f8863be7127d7bb04edc7beabcffb65b90162ad12bab46f69f33d03f15226159',
                           target_spp=spp,max_path_depth=depth,seed=seed)
    w=replace(template,scene_sha256=RAW,camera_node_index=5,width=width,height=height,
              integrator=ROOM_INTEGRATOR,lighting_policy=LIGHTING_POLICY,rendering_policy=RENDERING_POLICY,
              color_output_policy=COLOR_POLICY)
    validate(w);return w


def validate(w):
    if type(w) is not WorkloadSpec or w.scene_sha256!=RAW or w.camera_node_index!=5 or w.scene_index!=0:
        raise UnsupportedTransport('Original-room source/camera identity required; no fixture fallback.')
    if (w.width,w.height) not in ((160,90),(640,360)) or not 1<=w.target_spp<=8 or not 1<=w.max_path_depth<=3:
        raise UnsupportedTransport('Unsupported bounded original-room quality profile.')
    expected=make_template(w)
    if w!=expected:raise UnsupportedTransport('Original-room numerical policy differs.')
    return (0.,0.,0.),()


def make_template(w):
    tiny=tiny_workload('f8863be7127d7bb04edc7beabcffb65b90162ad12bab46f69f33d03f15226159',
        target_spp=w.target_spp,max_path_depth=w.max_path_depth,seed=w.seed,rr=w.termination_policy==RR_POLICY)
    return replace(tiny,scene_sha256=RAW,camera_node_index=5,width=w.width,height=w.height,
        integrator=ROOM_INTEGRATOR,lighting_policy=LIGHTING_POLICY,rendering_policy=RENDERING_POLICY,
        color_output_policy=COLOR_POLICY)
