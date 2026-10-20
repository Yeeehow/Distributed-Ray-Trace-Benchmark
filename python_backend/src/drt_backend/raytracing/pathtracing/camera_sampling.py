"""Addressed perspective camera, separate from the unchanged Legacy pixel centres."""
from dataclasses import dataclass, field
import math
import numpy as np

from drt_backend.raytracing.model.camera import CameraData
from drt_backend.raytracing.model.rays import Ray
from drt_backend.raytracing.tracing.primary_rays import _perspective
from drt_backend.raytracing.scene.transforms import validate_affine_matrix, numeric_array
from .sampling import uniform_sample
from .pbr_numeric import scalar, normalize
from .transport_errors import InvalidTransport


@dataclass(frozen=True, slots=True)
class PbrCamera:
    source: CameraData = field(compare=False, repr=False)
    width: int
    height: int
    position: tuple = field(init=False)
    rotation: tuple = field(init=False)
    scale: float = field(init=False)
    aspect: float = field(init=False)
    near: float = field(init=False)
    far: float | None = field(init=False)

    def __post_init__(self):
        if not isinstance(self.source, CameraData) or any(type(v) is not int or v <= 0 for v in (self.width, self.height)):
            raise InvalidTransport('Camera requires CameraData and positive native dimensions.')
        c = self.source
        p = _perspective(c)
        yfov = scalar(p.yfov, InvalidTransport, minimum=0)
        near = scalar(p.znear, InvalidTransport, minimum=0)
        far = None if p.zfar is None else scalar(p.zfar, InvalidTransport, minimum=0)
        if not 0 < yfov < math.pi or near <= 0 or far is not None and far <= near:
            raise InvalidTransport('Invalid perspective field of view or clipping planes.')
        aspect = self.width/self.height
        if p.aspect_ratio is not None:
            explicit = scalar(p.aspect_ratio, InvalidTransport, minimum=0)
            if explicit <= 0 or not math.isclose(aspect, explicit, rel_tol=1e-9, abs_tol=1e-12):
                raise InvalidTransport('Viewport aspect differs from the selected camera.')
            aspect = explicit
        pose = validate_affine_matrix(c.camera_pose)
        position = numeric_array(c.position, (3,), 'PBR camera position')
        rotation = pose[:3, :3]
        if (not np.allclose(rotation.T@rotation, np.eye(3), rtol=1e-6, atol=1e-6)
                or not np.isclose(np.linalg.det(rotation), 1, rtol=1e-6, atol=1e-6)
                or not np.allclose(position, pose[:3, 3], rtol=1e-12, atol=1e-12)):
            raise InvalidTransport('Camera needs a proper scale-free pose and consistent position.')
        for name, value in dict(position=tuple(float(x) for x in pose[:3, 3]),
                rotation=tuple(tuple(float(x) for x in row) for row in rotation),
                scale=math.tan(yfov/2), aspect=aspect, near=near, far=far).items():
            object.__setattr__(self, name, value)

    def at(self, pixel_id, ux, uy):
        """Projection reference at explicit subpixel uniforms; does not trace."""
        if type(pixel_id) is not int or not 0 <= pixel_id < self.width*self.height:
            raise InvalidTransport('PixelId is outside the row-major viewport.')
        ux = scalar(ux, InvalidTransport, minimum=0, maximum=math.nextafter(1.0, 0.0))
        uy = scalar(uy, InvalidTransport, minimum=0, maximum=math.nextafter(1.0, 0.0))
        x, y = pixel_id % self.width, pixel_id // self.width
        u = 2*(x+ux)/self.width-1
        v = 1-2*(y+uy)/self.height
        local = normalize((u*self.aspect*self.scale, v*self.scale, -1.0), InvalidTransport)
        world = normalize(tuple(math.fsum(a*b for a, b in zip(row, local)) for row in self.rotation), InvalidTransport)
        t_min = self.near/-local[2]
        t_max = math.inf if self.far is None else self.far/-local[2]
        return Ray(self.position, world, t_min, t_max)

    def addressed(self, seed, pixel_id, sample_id):
        return self.at(pixel_id, uniform_sample(seed, pixel_id, sample_id, 0),
                       uniform_sample(seed, pixel_id, sample_id, 1))
