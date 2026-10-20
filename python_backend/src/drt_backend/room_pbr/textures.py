"""Source-owned glTF texels: transfer before filtering, linear data unmodified."""
import math
import numpy as np
from drt_backend.raytracing.shading.texture_sampling import effective_filter, SamplingPolicy, _address
from drt_backend.raytracing.pathtracing.pbr_material import TextureSample, ResolvedTextureSamples

SLOTS=('base_color','metallic_roughness','specular','specular_color','emissive','normal')
COLOR={'base_color','specular_color','emissive'}


def sample(context,slot,uv,color):
    context.require_open()
    if slot.texcoord!=0 or slot.extensions:raise ValueError('Unadmitted texture UV/transform.')
    if uv is None:raise ValueError('Declared texture requires source TEXCOORD_0.')
    md=context.shaded.metadata;texture=md.textures[slot.texture_index]
    if texture.texture_index!=slot.texture_index or texture.extensions:raise ValueError('Texture identity/extension.')
    sampler=md.sampler_for_texture(slot.texture_index);image=context.image(texture.source_image_index)
    if sampler.extensions:raise ValueError('Sampler extension.')
    x,y=(float(uv[0])*image.width,float(uv[1])*image.height)
    if not all(math.isfinite(v) and abs(v)<=2**51 for v in (x,y)):raise ValueError('Invalid texture coordinates.')
    def texel(i,j):
        value=image.pixels[_address(j,image.height,sampler.wrap_t),_address(i,image.width,sampler.wrap_s)].astype(np.float64)/255.
        if color:
            c=value[:3];value[:3]=np.where(c<=.04045,c/12.92,((c+.055)/1.055)**2.4)
        return value
    if effective_filter(sampler,SamplingPolicy())=='nearest':value=texel(math.floor(x),math.floor(y))
    else:
        x-=.5;y-=.5;ix,iy=math.floor(x),math.floor(y);fx,fy=x-ix,y-iy
        value=(1-fx)*(1-fy)*texel(ix,iy)+fx*(1-fy)*texel(ix+1,iy)+(1-fx)*fy*texel(ix,iy+1)+fx*fy*texel(ix+1,iy+1)
    return TextureSample(slot.texture_index,tuple(float(c) for c in value),'linear' if color else 'data')


def resolve_samples(context,material,uv):
    return ResolvedTextureSamples(**{name:None if (slot:=getattr(material,name+'_texture')) is None
        else sample(context,slot,uv,name in COLOR) for name in SLOTS})
