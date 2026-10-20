"""Roulette conditions both current-vertex techniques before either is sampled."""
from dataclasses import dataclass
import math
from .sampling import uniform_sample
from .pbr_numeric import vector, scalar
from .transport_errors import InvalidTransport


class InvalidRoulette(InvalidTransport):
    """Nonfinite throughput or invalid roulette probability/address."""


@dataclass(frozen=True,slots=True)
class RouletteResult:
    survived: bool
    probability: float
    uniform: float
    throughput: tuple


def survival_probability(beta):
    beta=vector(beta,3,InvalidRoulette,minimum=0)
    return min(0.95,max(0.10,max(beta)))


def roulette(beta, *, seed,pixel_id,sample_id,bounce):
    if type(bounce) is not int or bounce!=2: raise InvalidRoulette('This policy applies only at bounce2.')
    beta=vector(beta,3,InvalidRoulette,minimum=0)
    q=survival_probability(beta)
    u=uniform_sample(seed,pixel_id,sample_id,16+16*bounce+7)
    survived=u<q
    weight=tuple(c/q for c in beta) if survived else (0.,0.,0.)
    return RouletteResult(survived,q,u,vector(weight,3,InvalidRoulette,minimum=0))
