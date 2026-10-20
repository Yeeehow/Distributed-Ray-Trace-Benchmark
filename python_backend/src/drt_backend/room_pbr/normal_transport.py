"""Reciprocal passive geometric-measure adapter for the existing local BSDF.

For positive geometric and shading cosines let a(w)=min(1, Ns.w/Ng.w).
Define f_g(wi,wo)=f_s(wi,wo)*a(wi)*a(wo). It is reciprocal and
integral f_g*cos_g <= integral f_s*cos_s <= 1. Old integrator terms
already multiply cos_s, so their factor is min(1,cos_gi/cos_si)*a(wo).
All ratios are evaluated only inside both strict positive hemispheres.
This explicitly conservative version is not the unbounded Veach adjoint ratio.
"""
from drt_backend.raytracing.pathtracing.pbr_numeric import dot


def correction(frame,wi,wo):
    ns,ng=frame.shading_normal,frame.oriented_geometric_normal
    si,so,gi,go=dot(ns,wi),dot(ns,wo),dot(ng,wi),dot(ng,wo)
    if min(si,so,gi,go)<=0:return 0.
    return min(1.,gi/si)*min(1.,so/go)
