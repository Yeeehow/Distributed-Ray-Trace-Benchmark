"""Oriented world-space frames, with geometric normal and hit owner retained.

Normal-map fallback is per-triangle UV derivatives, explicitly not MikkTSpace
mesh-wide tangent generation. The frozen normal-mapped scene has source tangents.
"""
from dataclasses import dataclass
import math
import numpy as np

from .pbr_errors import InvalidSurfaceFrame
from .pbr_numeric import vector, scalar, dot, cross, normalize, UNIT_TOLERANCE


@dataclass(frozen=True, slots=True)
class PbrSurfaceFrame:
    geometric_normal: tuple[float, float, float]  # original world Ng, never map-replaced
    shading_normal: tuple[float, float, float]  # oriented world Ns
    tangent: tuple[float, float, float]
    bitangent: tuple[float, float, float]
    handedness: int
    front_face: bool
    source_surface: object = None  # exact borrowed SurfaceAttributes in adapter

    def __post_init__(self):
        for name in ('geometric_normal', 'shading_normal', 'tangent', 'bitangent'):
            object.__setattr__(self, name, vector(getattr(self, name), 3, InvalidSurfaceFrame, unit=True))
        if type(self.front_face) is not bool or type(self.handedness) is not int or self.handedness not in (-1, 1):
            raise InvalidSurfaceFrame('Invalid face or tangent handedness.')
        t, b, n = self.tangent, self.bitangent, self.shading_normal
        if any(abs(dot(a, c)) > UNIT_TOLERANCE for a, c in ((t, b), (t, n), (b, n))):
            raise InvalidSurfaceFrame('Frame is not orthogonal.')
        if abs(dot(cross(t, b), n)-self.handedness) > 4*UNIT_TOLERANCE:
            raise InvalidSurfaceFrame('Frame handedness does not match its basis.')
        if dot(n, self.oriented_geometric_normal) <= 0:
            raise InvalidSurfaceFrame('Shading normal leaves the oriented geometric hemisphere.')

    @property
    def oriented_geometric_normal(self):
        sign = 1 if self.front_face else -1
        return tuple(sign*c for c in self.geometric_normal)

    def to_local(self, world_direction):
        direction = vector(world_direction, 3, InvalidSurfaceFrame, unit=True)
        return tuple(dot(direction, axis) for axis in (self.tangent, self.bitangent, self.shading_normal))

    def to_world(self, local_direction):
        direction = vector(local_direction, 3, InvalidSurfaceFrame, unit=True)
        return normalize(tuple(sum(direction[j]*axis[i] for j, axis in enumerate(
            (self.tangent, self.bitangent, self.shading_normal))) for i in range(3)), InvalidSurfaceFrame)

    def valid_reflection(self, wi_world, wo_world) -> bool:
        wi = vector(wi_world, 3, InvalidSurfaceFrame, unit=True)
        wo = vector(wo_world, 3, InvalidSurfaceFrame, unit=True)
        return all(dot(d, n) > 0 for d in (wi, wo) for n in (
            self.shading_normal, self.oriented_geometric_normal))


def build_surface_frame(geometric_normal_world, shading_normal_local, world_linear, *,
                        front_face: bool, double_sided: bool, tangent_local=None,
                        positions_local=None, triangle_uvs=None, normal_rgb=None,
                        normal_scale=1.0, source_surface=None) -> PbrSurfaceFrame:
    """Transform Ns by inverse transpose, T by A, reorthogonalize and retain Ng.

    Imported tangent w combines with sign(det A). Back-face Ns/B reverse; T
    remains UV-oriented. Missing source tangents require triangle UV derivatives
    for any normal map; an arbitrary ONB is allowed only with no normal map.
    """
    ng = vector(geometric_normal_world, 3, InvalidSurfaceFrame, unit=True)
    if type(front_face) is not bool or type(double_sided) is not bool or not front_face and not double_sided:
        raise InvalidSurfaceFrame('A back-facing single-sided surface is not a shading surface.')
    try:
        matrix = np.asarray(world_linear)
        if matrix.shape != (3, 3) or matrix.dtype.kind not in 'iuf':
            raise InvalidSurfaceFrame('World linear transform requires a real 3x3 matrix.')
        matrix = matrix.astype(np.float64)
        if not np.isfinite(matrix).all():
            raise InvalidSurfaceFrame('Transform is not finite.')
        condition = float(np.linalg.cond(matrix))
        sign, _ = np.linalg.slogdet(matrix)
        if not math.isfinite(condition) or condition > 1e12 or sign == 0:
            raise InvalidSurfaceFrame('Singular/ill-conditioned transform (condition > 1e12).')
        with np.errstate(over='raise', invalid='raise', divide='raise'):
            normal_local = vector(shading_normal_local, 3, InvalidSurfaceFrame)
            n = normalize(np.linalg.solve(matrix.T, normal_local), InvalidSurfaceFrame)
            alignment = 1 if dot(n, ng) >= 0 else -1
            n = tuple(c*alignment for c in n)
            hand = 1
            if tangent_local is not None:
                tangent = vector(tangent_local, 4, InvalidSurfaceFrame)
                if tangent[3] not in (-1.0, 1.0):
                    raise InvalidSurfaceFrame('glTF tangent.w must be exactly -1 or +1.')
                raw_t = tuple(matrix @ np.asarray(tangent[:3]))
                t = normalize(tuple(raw_t[i]-dot(raw_t, n)*n[i] for i in range(3)), InvalidSurfaceFrame)
                hand = int(tangent[3]*sign*alignment)
            elif normal_rgb is not None:
                positions = np.asarray(positions_local)
                uv = np.asarray(triangle_uvs)
                if (positions.shape != (3, 3) or uv.shape != (3, 2)
                        or positions.dtype.kind not in 'iuf' or uv.dtype.kind not in 'iuf'
                        or not np.isfinite(positions).all() or not np.isfinite(uv).all()):
                    raise InvalidSurfaceFrame('Normal mapping requires source tangent or finite triangle/UV data.')
                e1, e2 = matrix @ (positions[1]-positions[0]), matrix @ (positions[2]-positions[0])
                du, dv = uv[1]-uv[0], uv[2]-uv[0]
                determinant = float(du[0]*dv[1]-du[1]*dv[0])
                if abs(determinant) <= 1e-14:
                    raise InvalidSurfaceFrame('Degenerate UV determinant (absolute value <= 1e-14).')
                raw_t = (e1*dv[1]-e2*du[1])/determinant
                raw_b = (e2*du[0]-e1*dv[0])/determinant
                t = normalize(tuple(raw_t[i]-dot(raw_t, n)*n[i] for i in range(3)), InvalidSurfaceFrame)
                orientation = dot(cross(n, t), normalize(raw_b, InvalidSurfaceFrame))
                if abs(orientation) <= 1e-12:
                    raise InvalidSurfaceFrame('Degenerate UV tangent orientation.')
                hand = 1 if orientation > 0 else -1
            else:
                # A BSDF with no normal texture has no required UV orientation.
                axis = min(range(3), key=lambda i: abs(n[i]))
                reference = tuple(float(i == axis) for i in range(3))
                t = normalize(cross(reference, n), InvalidSurfaceFrame)
            if not front_face:
                n = tuple(-c for c in n)
            b = tuple(hand*c for c in cross(n, t))
            if normal_rgb is not None:
                rgb = vector(normal_rgb, 3, InvalidSurfaceFrame, minimum=0, maximum=1)
                scale = scalar(normal_scale, InvalidSurfaceFrame)
                mapped = normalize(((2*rgb[0]-1)*scale, (2*rgb[1]-1)*scale, 2*rgb[2]-1), InvalidSurfaceFrame)
                n = normalize(tuple(mapped[0]*t[i]+mapped[1]*b[i]+mapped[2]*n[i] for i in range(3)), InvalidSurfaceFrame)
                if dot(n, tuple(c*(1 if front_face else -1) for c in ng)) <= 0:
                    raise InvalidSurfaceFrame('Mapped normal must stay in the oriented geometric hemisphere.')
                t = normalize(tuple(t[i]-dot(t, n)*n[i] for i in range(3)), InvalidSurfaceFrame)
                b = tuple(hand*c for c in cross(n, t))
    except (ValueError, TypeError, OverflowError, FloatingPointError, np.linalg.LinAlgError) as cause:
        if isinstance(cause, InvalidSurfaceFrame):
            raise
        raise InvalidSurfaceFrame('Frame calculation is not finite/representable.') from cause
    return PbrSurfaceFrame(ng, n, t, b, hand, front_face, source_surface)


def frame_from_surface(surface, *, double_sided: bool, normal_rgb=None, normal_scale=1.0):
    """Read-only adapter, retaining the exact existing SurfaceAttributes owner."""
    from drt_backend.raytracing.tracing.surface_evaluation import SurfaceAttributes
    if type(surface) is not SurfaceAttributes:
        raise InvalidSurfaceFrame('Expected existing SurfaceAttributes.')
    primitive = surface.primitive
    positions = primitive.positions[list(surface.vertex_indices)]
    uv = None if primitive.texcoord_0 is None else primitive.texcoord_0[list(surface.vertex_indices)]
    normal = surface.vertex_normal_local
    if normal is None or surface.normal_source != 'smooth':
        normal = np.cross(positions[1]-positions[0], positions[2]-positions[0])
    tangent = None
    if primitive.tangents is not None:
        tangents = primitive.tangents[list(surface.vertex_indices)]
        signs = tangents[:, 3]
        if any(s not in (-1, 1) for s in signs) or any(s != signs[0] for s in signs):
            raise InvalidSurfaceFrame('Triangle tangent signs are inconsistent.')
        xyz = np.asarray(surface.barycentric_weights) @ tangents[:, :3]
        tangent = (*xyz, float(signs[0]))
    return build_surface_frame(surface.geometric_normal_world, normal, surface.instance.world_matrix[:3, :3],
        front_face=surface.hit.front_face, double_sided=double_sided, tangent_local=tangent,
        positions_local=positions, triangle_uvs=uv, normal_rgb=normal_rgb,
        normal_scale=normal_scale, source_surface=surface)
