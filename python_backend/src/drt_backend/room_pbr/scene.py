"""Original source through the existing GLB/ECS/BVH/texture/contact owners."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from drt_backend.raytracing.scene.glb_loader import load_glb, _container
from drt_backend.raytracing.ecs.components import asset_fingerprint
from drt_backend.raytracing.resources.preparation import prepare_scene
from drt_backend.raytracing.tracing.primary_rays import select_camera
from drt_backend.raytracing.pathtracing.camera_sampling import PbrCamera
from drt_backend.raytracing.pathtracing.scene_adapter import PbrSceneAdapter,PbrSurface
from drt_backend.raytracing.pathtracing.multibounce_scene import MultiScene
from drt_backend.raytracing.pathtracing.pbr_material import parse_pbr_source,resolve_surface_material
from drt_backend.raytracing.pathtracing.surface_frame import frame_from_surface
from .policy import RAW,validate
from .textures import resolve_samples,SLOTS
from .normal_transport import correction


@dataclass(frozen=True,slots=True)
class RoomSource:
    raw_sha256:str
    ecs_sha256:str
    asset:object
    raw_bytes:bytes

    @classmethod
    def load(cls,path,w):
        validate(w);raw=Path(path).read_bytes()
        if sha256(raw).hexdigest()!=RAW:raise ValueError('Exact original room SHA256 required.')
        asset=load_glb(path);text,binary=_container(raw)
        if json.loads(text)!=asset.raw_document or binary!=asset.binary:raise ValueError('Source changed while loading.')
        for i,m in enumerate(asset.raw_document.get('materials',())):
            material=parse_pbr_source(m,i,len(asset.document.textures))
            if material.alpha_mode!='OPAQUE':raise ValueError('Room alpha subset must be OPAQUE.')
            if material.occlusion_texture is not None:raise ValueError('Occlusion transport unsupported.')
            for slot in material.texture_slots:
                if slot.texcoord!=0 or slot.extensions:raise ValueError('Room texture coordinates/extension unsupported.')
        return cls(RAW,asset_fingerprint(asset),asset,raw)

    def identity(self,scene_index):return f'pbr-raw:{self.raw_sha256}:ecs-v1:{self.ecs_sha256}:scene:{scene_index}'


@dataclass(frozen=True,slots=True,eq=False)
class RoomScene(MultiScene):
    @classmethod
    def prepare(cls,source,w,*,checkpoint=lambda:None):
        validate(w)
        if type(source) is not RoomSource or source.raw_sha256!=RAW or asset_fingerprint(source.asset)!=source.ecs_sha256:
            raise ValueError('Room source/ECS ownership differs.')
        prepared=prepare_scene(source.asset,scene_index=0,checkpoint=checkpoint)
        try:
            md=prepared.metadata;images=[]
            for material in md.materials:
                for name in SLOTS:
                    slot=getattr(material,name+'_texture')
                    if slot is not None:images.append(md.textures[slot.texture_index].source_image_index)
            prepared.textures.prepare(images)
            if sorted(l.source.type for l in prepared.lighting.lights)!=['point','point','point','spot','spot']:
                raise ValueError('Original five punctual light types differ.')
            if any(l.range is not None for l in prepared.lighting.lights):raise ValueError('Unexpected light range.')
            camera=PbrCamera(select_camera(md,5),w.width,w.height)
            return cls(source,w,prepared,camera,())
        except BaseException:prepared.close();raise

    def resolve(self,ray,accepted):
        self._owned(ray,accepted)
        samples=resolve_samples(self.prepared.textures,accepted.material,accepted.surface.texcoord_0)
        material=resolve_surface_material(accepted.material,samples,accepted.surface)
        frame=frame_from_surface(accepted.surface,double_sided=material.double_sided,
                                normal_rgb=material.normal_rgb,normal_scale=material.normal_scale)
        return PbrSurface(accepted,material,frame)

    @staticmethod
    def transport_correction(frame,wi,wo):return correction(frame,wi,wo)
