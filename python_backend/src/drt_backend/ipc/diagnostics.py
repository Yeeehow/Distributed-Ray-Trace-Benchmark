"""Opt-in bounded failure context for the explicit long acceptance policy only."""
from dataclasses import asdict
from fractions import Fraction
import math

import numpy as np


def _value(value, depth=0):
    if depth>5: return '<depth bound>'
    if value is None or type(value) in (str,bool,int): return value[:2048] if isinstance(value,str) else value
    if isinstance(value,Fraction): return dict(exact=str(value),approximate=float(value))
    if isinstance(value,(float,np.floating)): return float(value) if math.isfinite(value) else str(value)
    if isinstance(value,np.ndarray): return _value(value.tolist(),depth+1) if value.size<=16 else dict(shape=value.shape)
    if isinstance(value,(list,tuple)) and len(value)<=16: return [_value(item,depth+1) for item in value]
    return '<'+type(value).__name__+'>'


def _ray(ray):
    return {key:_value(getattr(ray,key)) for key in ('origin','direction','t_min','t_max')}


def _triangle(context,hit):
    instance,primitive,prepared=context.resolve_hit(hit)
    indices=primitive.triangle_indices[hit.triangle_index]
    return dict(hit=asdict(hit),node=instance.node_index,mesh=instance.mesh_index,vertex_indices=indices.tolist(),
        local=primitive.positions[indices].tolist(),world_matrix=instance.world_matrix.tolist(),
        inverse_linear=prepared.inverse_linear.tolist())


def failure_context(failure,width,height):
    frames={};locations=[];tb=failure.__traceback__
    while tb is not None and len(locations)<32:
        name=tb.tb_frame.f_code.co_name;frames[name]=tb.tb_frame.f_locals
        locations.append(dict(function=name,line=tb.tb_lineno));tb=tb.tb_next
    result=dict(error=type(failure).__name__,message=str(failure)[:4096],width=width,height=height,frames=locations)
    if 'execute_range' in frames:
        pixel=int(frames['execute_range'].get('pixel',-1));result.update(pixel=pixel,x=pixel%width,y=pixel//width)
    if '_shade_pixel' in frames: result['primary']=_ray(frames['_shade_pixel']['ray'])
    if 'visibility' in frames:
        scope=frames['visibility'];accepted=scope['accepted'];sample=scope['sample'];context=scope['context'].shaded
        result['source']=_triangle(context,accepted.hit)
        result['surface']={key:_value(getattr(accepted.surface,key)) for key in ('position_world','position_ray_world','geometric_normal_world','shading_normal_world')}
        result['light']=dict(index=sample.light.source.light_index,node=sample.light.source.node_index,
            position=_value(sample.light.position),incoming=_value(sample.incoming_direction))
        if 'spawned' in scope:
            spawned=scope['spawned'];result['shadow']=_ray(spawned.ray);result['contact_ray']=_ray(spawned.guard.contact_ray)
            result['envelope']=dict(near_t=spawned.guard.near_t,position_error_l1=spawned.position_error_l1,
                offset_distance=spawned.offset_distance,endpoint_margin=spawned.endpoint_margin)
    if '_shadow_geometric_candidates' in frames:
        scope=frames['_shadow_geometric_candidates']
        if 'hit' in scope: result['contact']=_triangle(scope['context'].shaded,scope['hit'])
    if '_spawn' in frames:
        keys=('vertices','a','translation','norm_a','norm_inv','extents','extent','smallest','plane','tau','exact_point','u','v',
            'p','bary_world','ray_exact_t','g5','g6','local_error','errors','error_l1','delta','shifted','signed_distance')
        result['spawn_calculation']={key:_value(value) for key,value in frames['_spawn'].items() if key in keys}
    return result
