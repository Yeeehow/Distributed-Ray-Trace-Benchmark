"""One owned existing PreparedScene; no second scene, BVH or intersection kernel.

The continuation bridge intentionally calls the audited private Legacy _spawn:
its exact arithmetic accepts a direction/endpoint carrier, independent of light
units. Public spawn_shadow_ray is retained unchanged for actual metadata lights.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import math

from drt_backend.raytracing.scene.glb_loader import GlbAsset, load_glb, _container
from drt_backend.raytracing.ecs.components import asset_fingerprint
from drt_backend.raytracing.resources.preparation import prepare_scene, PreparedScene
from drt_backend.raytracing.tracing.primary_rays import select_camera
from drt_backend.raytracing.tracing.surface_queries import (AcceptedSurface, trace_filtered_closest,
    _shadow_geometric_candidates, _evaluate_camera_candidate, ShadowNumericalError)
from drt_backend.raytracing.shading.shadow_visibility import _spawn, spawn_shadow_ray
from drt_backend.raytracing.shading.texture_sampling import SamplingPolicy
from drt_backend.raytracing.shading.direct_lighting import sample_light
from drt_backend.raytracing.model.rays import Ray
from .camera_sampling import PbrCamera
from .pbr_material import ResolvedTextureSamples, resolve_surface_material, parse_pbr_source, resolve_material
from .surface_frame import frame_from_surface
from .pbr_numeric import vector
from .transport_policy import validate_workload, ORIGINAL_SHA256
from .transport_errors import InvalidTransport, UnsupportedTransport


@dataclass(frozen=True, slots=True)
class PbrSource:
    """Owned loaded GLB snapshot, read-only metadata by existing scene convention."""
    raw_sha256: str
    ecs_sha256: str
    asset: GlbAsset
    raw_bytes: bytes = field(repr=False)

    def __post_init__(self):
        if (type(self.raw_bytes) is not bytes or not isinstance(self.asset, GlbAsset)
                or sha256(self.raw_bytes).hexdigest() != self.raw_sha256
                or self.raw_sha256 == ORIGINAL_SHA256 or asset_fingerprint(self.asset) != self.ecs_sha256):
            raise InvalidTransport('Source raw bytes, hash or parsed ECS fingerprint disagree.')
        text, binary = _container(self.raw_bytes)
        if binary != self.asset.binary or json.loads(text) != self.asset.raw_document:
            raise InvalidTransport('Source parsed geometry/metadata differ from its raw bytes.')

    @classmethod
    def load(cls, path, workload):
        validate_workload(workload)
        data = Path(path).read_bytes()
        digest = sha256(data).hexdigest()
        if digest == ORIGINAL_SHA256 or digest != workload.scene_sha256:
            raise InvalidTransport('Raw GLB SHA256 differs from admitted synthetic workload.')
        asset = load_glb(path)
        text, binary = _container(data)
        if binary != asset.binary or json.loads(text) != asset.raw_document:
            raise InvalidTransport('GLB changed during source loading.')
        # Explicitly reject every declared unresolved slot, including unused ones.
        for i, raw in enumerate(asset.raw_document.get('materials', ())):
            material = parse_pbr_source(raw, i, len(asset.document.textures))
            if material.texture_slots:
                raise UnsupportedTransport('M6.2 has no integrated texture resolver; declared slots cannot be neutralized.')
            if material.alpha_mode != 'OPAQUE':
                raise UnsupportedTransport('MASK/BLEND transport is not implemented.')
            resolve_material(material, ResolvedTextureSamples())
        for mesh in asset.raw_document.get('meshes', ()):
            for primitive in mesh['primitives']:
                if 'COLOR_0' in primitive['attributes']:
                    raise UnsupportedTransport('Integrated vertex-color resolution is deferred.')
        return cls(digest, asset_fingerprint(asset), asset, data)

    def identity(self, scene_index):
        return f'pbr-raw:{self.raw_sha256}:ecs-v1:{self.ecs_sha256}:scene:{scene_index}'


@dataclass(frozen=True, slots=True)
class PbrSurface:
    accepted: AcceptedSurface
    material: object
    frame: object


@dataclass(frozen=True, slots=True)
class _InfiniteEndpoint:
    position: None = None


@dataclass(frozen=True, slots=True)
class _DirectionCarrier:
    incoming_direction: tuple
    light: _InfiniteEndpoint = _InfiniteEndpoint()


@dataclass(frozen=True, slots=True, eq=False)
class PbrSceneAdapter:
    """Own exactly one PreparedScene; close is idempotent, including on failure."""
    source: PbrSource
    workload: object
    prepared: PreparedScene
    camera: PbrCamera

    @classmethod
    def prepare(cls, source, workload, *, checkpoint=lambda: None):
        validate_workload(workload)
        if (type(source) is not PbrSource or source.raw_sha256 != workload.scene_sha256
                or asset_fingerprint(source.asset) != source.ecs_sha256):
            raise InvalidTransport('Prepared source snapshot changed or does not bind to workload.')
        prepared = prepare_scene(source.asset, scene_index=workload.scene_index, checkpoint=checkpoint)
        try:
            if prepared.components.namespace.asset_sha256 != source.ecs_sha256:
                raise InvalidTransport('Prepared ECS namespace differs from declared resource.')
            camera = PbrCamera(select_camera(prepared.metadata, workload.camera_node_index), workload.width, workload.height)
            lights = prepared.lighting.lights
            if len(lights) > 1 or any(light.source.type != 'point' or light.range is not None for light in lights):
                raise UnsupportedTransport('Only zero or one metadata point light without range is supported.')
            checkpoint()
            return cls(source, workload, prepared, camera)
        except BaseException:
            prepared.close()
            raise

    def close(self):
        self.prepared.close()

    def _owned(self, ray, accepted):
        self.prepared.require_open()
        if not isinstance(ray, Ray) or not isinstance(accepted, AcceptedSurface):
            raise InvalidTransport('Source requires actual Ray and AcceptedSurface.')
        ctx = self.prepared.textures.shaded
        hit, surface = accepted.hit, accepted.surface
        instance, primitive, inverse = ctx.resolve_hit(hit)
        if (surface.scene is not ctx.scene or surface.instance is not instance or surface.primitive is not primitive
                or accepted.material is not ctx.metadata.material_for_primitive(primitive)):
            raise InvalidTransport('Surface/scene/material identity differs from prepared owner.')
        if (hit.triangle_index >= len(primitive.triangle_indices) or not ray.t_min <= hit.t <= ray.t_max
                or surface.vertex_indices != tuple(int(i) for i in primitive.triangle_indices[hit.triangle_index])
                or surface.barycentric_weights != (1-hit.u-hit.v, hit.u, hit.v)):
            raise ShadowNumericalError('Source hit/index/interval is inconsistent.')
        return instance, primitive, inverse

    def resolve(self, ray, accepted):
        self._owned(ray, accepted)
        material = resolve_surface_material(accepted.material, ResolvedTextureSamples(), accepted.surface)
        if material.alpha_mode != 'OPAQUE':
            raise UnsupportedTransport('Unsupported alpha transport.')
        frame = frame_from_surface(accepted.surface, double_sided=material.double_sided,
                                   normal_rgb=material.normal_rgb, normal_scale=material.normal_scale)
        if any(abs(a-b) > 2e-12 for a, b in zip(frame.shading_normal, frame.oriented_geometric_normal)):
            raise UnsupportedTransport('Ns differs from oriented Ng; shading-normal transport correction is undefined.')
        return PbrSurface(accepted, material, frame)

    def closest(self, ray, guard=None):
        self.prepared.require_open()
        texture = self.prepared.textures
        if guard is None:
            accepted = trace_filtered_closest(texture, ray)
        else:
            # Existing guarded geometric stream performs exact contact/departure
            # certification and shares its existing 65,536 candidate cap.
            candidates = []
            for hit in _shadow_geometric_candidates(texture, ray, guard, max_candidates=65_536):
                item = _evaluate_camera_candidate(texture, ray, hit, SamplingPolicy())
                if item is not None:
                    candidates.append(item)
            if not candidates:
                return None
            minimum = min(item.hit.t for item in candidates)
            accepted = min((item for item in candidates if abs(item.hit.t-minimum) <=
                8*max(math.ulp(item.hit.t), math.ulp(minimum))),
                key=lambda item: (item.hit.instance_index, item.hit.primitive_index, item.hit.triangle_index))
        return None if accepted is None else self.resolve(ray, accepted)

    def spawn_continuation(self, primary, surface, direction):
        instance, primitive, inverse = self._owned(primary, surface.accepted)
        outgoing = vector(direction, 3, InvalidTransport, unit=True)
        return _spawn(self.prepared.textures.shaded, primary, surface.accepted,
                      _DirectionCarrier(outgoing), instance, primitive, inverse)

    def point_samples(self, surface):
        # Audited Legacy sample_light applies only color*intensity/distance^2
        # in this admitted no-range point subset. Our policy explicitly assigns
        # synthetic radiant-intensity units; no candela/lumen conversion occurs.
        for light in self.prepared.lighting.lights:
            sample = sample_light(light, surface.accepted.surface.position_world)
            if sample is not None:
                yield sample

    def spawn_shadow(self, primary, surface, sample):
        self._owned(primary, surface.accepted)
        return spawn_shadow_ray(self.prepared.textures.shaded, primary, surface.accepted, sample)
