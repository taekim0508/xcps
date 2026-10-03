"""Small builders for hand-made records used by unit tests."""

from traffic_cps.contracts import (
    DecisionContext,
    Direction,
    MeasurementQuality,
    Mode,
    Observation,
    SignalColor,
    SignalPhase,
)

NS, WE = Direction.NS, Direction.WE


def observation(time=10, ns_score=0.0, we_score=0.0, ns_count=0, we_count=0, green=NS, elapsed=10,
                target=None):
    """Observation with direction totals split evenly across the two lanes."""
    if target is not None:
        phase, current, colors = SignalPhase.yellow(target.other), None, {target.other: SignalColor.YELLOW,
                                                                         target: SignalColor.RED}
    else:
        phase, current, colors = SignalPhase.green(green), green, {green: SignalColor.GREEN,
                                                                   green.other: SignalColor.RED}
    lanes = {"N_J_0": ns_count - ns_count // 2, "N_J_1": ns_count // 2,
             "W_J_0": we_count - we_count // 2, "W_J_1": we_count // 2}
    return Observation(
        observation_id=f"obs-{time}", time=time, counts_by_lane=lanes,
        counts_by_direction={NS: ns_count, WE: we_count},
        lane_scores={"N_J_0": ns_score / 2, "N_J_1": ns_score / 2, "W_J_0": we_score / 2, "W_J_1": we_score / 2},
        scores={NS: ns_score, WE: we_score}, colors=colors,
        color_since_s={NS: time - (elapsed or 0), WE: time - (elapsed or 0)},
        phase=phase, current_green=current, elapsed_green_s=None if target else elapsed,
        transition_target=target, transition_start_s=time if target else None,
        raw_signal="GGrr", measurement=MeasurementQuality(1, ()),
    )


def context(obs, mode=Mode.EVALUATE):
    return DecisionContext(
        observation=obs, allowed=(NS, WE), mode=mode, minimum_green_s=5, green_service_limit_s=60,
        tie_abs_tolerance=1e-9, score_cap=2.0,
    )
