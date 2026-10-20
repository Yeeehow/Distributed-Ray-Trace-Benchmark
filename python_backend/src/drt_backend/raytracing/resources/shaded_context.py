"""Small M4 evaluation boundary borrowing an existing scene and acceleration."""

from dataclasses import dataclass

from drt_backend.raytracing.tracing.bvh import SceneAcceleration
from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, require_int
from drt_backend.raytracing.scene.geometry import PrimitiveGeometry
from drt_backend.raytracing.tracing.intersection import HitRecord, PreparedInstance
from drt_backend.raytracing.scene.render_metadata import RenderMetadata
from drt_backend.raytracing.scene.scene import MeshInstance, Scene


@dataclass(frozen=True, slots=True, eq=False)
class ShadedContext:
    """Borrow builder-produced inputs read-only; own no geometry or BVH copies.

    The source scene/metadata are immutable by convention, not deep-frozen.
    Callers must not mutate them while this context or its results are in use.
    """

    acceleration: SceneAcceleration
    metadata: RenderMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.acceleration, SceneAcceleration) or not isinstance(self.metadata, RenderMetadata):
            raise RenderValidationError("Surface context requires SceneAcceleration and RenderMetadata.")
        if self.metadata.scene is not self.acceleration.scene:
            raise RenderValidationError("Surface context scene ownership does not match.")
        for mesh_index, blas in self.acceleration.blas_by_mesh.items():
            if self.scene.mesh_definitions.get(mesh_index) is not blas.definition:
                raise GeometryError("Surface context BLAS does not belong to its scene.")
        for index, prepared in self.acceleration.prepared_instances.items():
            require_int(index, "Prepared instance index", error=GeometryError)
            if (index >= len(self.scene.mesh_instances) or prepared.instance_index != index
                    or prepared.instance is not self.scene.mesh_instances[index]):
                raise GeometryError("Surface context prepared instance does not belong to its scene.")

    @property
    def scene(self) -> Scene:
        return self.acceleration.scene

    def require_primitive(self, primitive: PrimitiveGeometry) -> None:
        """Require the original object, not an equal-looking foreign primitive."""
        if not isinstance(primitive, PrimitiveGeometry):
            raise GeometryError("Surface material requires a PrimitiveGeometry.")
        require_int(primitive.mesh_index, "Primitive mesh index", error=GeometryError)
        require_int(primitive.primitive_index, "Primitive index", error=GeometryError)
        definition = self.scene.mesh_definitions.get(primitive.mesh_index)
        if (definition is None or definition.mesh_index != primitive.mesh_index
                or primitive.primitive_index >= len(definition.primitives)
                or definition.primitives[primitive.primitive_index] is not primitive):
            raise GeometryError("Primitive does not belong to the surface context scene.")

    def resolve_hit(self, hit: HitRecord) -> tuple[MeshInstance, PrimitiveGeometry, PreparedInstance]:
        """Resolve original instance/primitive ordinals and cached transforms."""
        if not isinstance(hit, HitRecord):
            raise RenderValidationError("Surface evaluation requires a HitRecord.")
        if hit.instance_index >= len(self.scene.mesh_instances):
            raise GeometryError("Surface hit instance_index is out of range.")
        instance = self.scene.mesh_instances[hit.instance_index]
        if self.scene.mesh_definitions.get(instance.mesh_index) is not instance.definition:
            raise GeometryError("Surface hit instance has a foreign MeshDefinition.")
        prepared = self.acceleration.prepared_instances.get(hit.instance_index)
        if prepared is None or prepared.instance is not instance:
            raise GeometryError("Surface hit has no matching prepared instance.")
        if hit.primitive_index >= len(instance.definition.primitives):
            raise GeometryError("Surface hit primitive_index is out of range.")
        primitive = instance.definition.primitives[hit.primitive_index]
        self.require_primitive(primitive)
        return instance, primitive, prepared
