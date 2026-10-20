"""Narrow exact certificate for distinct triangles on the source's common plane.

Every Fraction is constructed from the stored binary float, before affine
evaluation. Adjacency, approximate distance, material and triangle identity do
not establish this certificate. No epsilon or rounded world-vertex comparison.
"""
from fractions import Fraction as F


def _sub(a, b):
    return tuple(x-y for x,y in zip(a,b,strict=True))


def _dot(a, b):
    return sum((x*y for x,y in zip(a,b,strict=True)), F(0))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _world_vertices(context, hit):
    instance, primitive, _ = context.resolve_hit(hit)
    matrix = tuple(tuple(F(float(x)) for x in row) for row in instance.world_matrix[:3])
    points = primitive.positions[primitive.triangle_indices[hit.triangle_index]]
    return tuple(tuple(_dot(row[:3],tuple(F(float(x)) for x in point))+row[3] for row in matrix) for point in points)


def certified_coplanar_departure(context, guard, hit):
    """Prove this distinct candidate cannot intersect the outgoing half-ray.

    If all candidate vertices are exactly on source plane N.(x-a)=0,
    s=N.(o-a) !=0 and s*(N.d)>0, then for every t>=0,
    N.(o+t*d-a)=s+t*(N.d) has the same strict sign. Thus no point of
    the candidate can block the shadow ray. A reverse contact segment may
    legitimately cross that common plane; this is not a swallowed blocker.
    Noncoplanar, separated, parallel-on-plane or inward cases remain uncertified.
    """
    a,b,c = _world_vertices(context, guard.source_hit)
    normal = _cross(_sub(b,a), _sub(c,a))
    if not any(normal):
        return False
    candidate = _world_vertices(context, hit)
    if not all(_dot(normal,_sub(vertex,a)) == 0 for vertex in candidate):
        return False
    side = _dot(normal,_sub(tuple(F(float(x)) for x in guard.ray.origin),a))
    slope = _dot(normal,tuple(F(float(x)) for x in guard.ray.direction))
    return side*slope > 0
