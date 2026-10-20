"""One prepared scene owner, retaining the existing geometry/BVH/metadata model."""
from dataclasses import dataclass, field
from os import PathLike
from collections.abc import Callable

from drt_backend.raytracing.tracing.bvh import SceneAcceleration, build_acceleration
from drt_backend.raytracing.ecs.components import SceneComponents, build_components
from drt_backend.raytracing.shading.direct_lighting import PreparedLighting
from drt_backend.raytracing.common.errors import RenderValidationError
from drt_backend.raytracing.scene.glb_loader import GlbAsset, load_glb
from drt_backend.raytracing.resources.image_decoding import ImageLimits, TextureContext
from drt_backend.raytracing.scene.render_metadata import RenderMetadata, build_render_metadata
from drt_backend.raytracing.scene.scene import Scene, build_scene
from drt_backend.raytracing.resources.shaded_context import ShadedContext


@dataclass(frozen=True, slots=True, eq=False)
class PreparedScene:
    """Prepare mutably, then borrow read-only until close.

    Scene and acceleration remain the single authoritative compatibility records.
    Components reference their original matrices/primitives; no instance geometry
    is copied. Metadata are shallow immutable records, not deeply frozen objects.
    close() releases the image cache; borrowed immutable arrays can outlive it.
    """
    scene: Scene
    metadata: RenderMetadata
    acceleration: SceneAcceleration
    components: SceneComponents
    textures: TextureContext
    lighting: PreparedLighting
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def asset(self) -> GlbAsset:
        return self.scene.asset

    def require_open(self) -> None:
        if self._closed:
            raise RenderValidationError("PreparedScene is closed.")
        self.textures.require_open()

    def close(self) -> None:
        if not self._closed:
            self.textures.close()
            object.__setattr__(self, "_closed", True)

    def __enter__(self) -> "PreparedScene":
        self.require_open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def prepare_scene(source: Scene | GlbAsset | str | PathLike, *, scene_index: int | None = None,
                  metadata: RenderMetadata | None = None,
                  acceleration: SceneAcceleration | None = None,
                  image_limits: ImageLimits = ImageLimits(),
                  checkpoint: Callable[[], None] | None = None) -> PreparedScene:
    """Own one explicit texture cache, lighting preparation and component projection.

    A caller may supply existing scene/metadata/acceleration objects. Their object
    identity is retained, including prepared inverse and BLAS/TLAS associations.
    No execution, scheduling, output publication or global session cache occurs.
    """
    if checkpoint is not None:
        checkpoint()
    if isinstance(source, Scene):
        scene = source
        if scene_index is not None and scene_index != scene.scene_index:
            raise RenderValidationError("Prepared scene index does not match its source.")
    else:
        asset = source if isinstance(source, GlbAsset) else load_glb(source)
        if checkpoint is not None:
            checkpoint()
        scene = build_scene(asset, scene_index=scene_index)
    if checkpoint is not None:
        checkpoint()
    metadata = build_render_metadata(scene) if metadata is None else metadata
    if checkpoint is not None:
        checkpoint()
    acceleration = build_acceleration(scene) if acceleration is None else acceleration
    if checkpoint is not None:
        checkpoint()
    shaded = ShadedContext(acceleration, metadata)
    if shaded.scene is not scene:
        raise RenderValidationError("Prepared resources do not belong to the supplied Scene.")
    textures = TextureContext(shaded, image_limits)
    try:
        textures.prepare()
        if checkpoint is not None:
            checkpoint()
        lighting = PreparedLighting(shaded)
        components = build_components(scene, metadata)
        if checkpoint is not None:
            checkpoint()
        return PreparedScene(scene, metadata, acceleration, components, textures, lighting)
    except BaseException:
        textures.close()
        raise
