from typing import List, Literal, Optional, Tuple, TypedDict
from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, Field, ValidationError
from dataclasses import dataclass
from redis.asyncio.client import PubSub
from redis.asyncio import Redis
from itgs import Itgs
import secrets
import asyncio
import time


CONNECT_TIMEOUT = 10
"""How long the client has to finish building the websocket, i.e. for websocket.accept() to be called."""
AUTH_TIMEOUT = 5
"""How long the client has to send the authentication packet after connecting."""
TRACE_EXISTS_TIMEOUT = 30
"""How long after the client connects before the progress bar trace must actually exist"""
SERVER_IDLE_TIMEOUT = 60 * 15
"""How long of receiving no updates on the trace from redis before the server closes the connection"""
SEND_TIMEOUT = 5
"""maximum amount of time spend sending a message"""
JOB_STATS_TIMEOUT = 300
"""Maximum amount of time to wait for the stats job to finish calculating statistics"""

router = APIRouter()


class AuthorizationPacket(BaseModel):
    """The packet sent by the client to authorize itself."""

    sub: str = Field(description="the subject of the user")
    progress_bar_name: str = Field(description="the name of the progress bar")
    progress_bar_trace_uid: str = Field(description="the uid of the progress bar trace")


class AuthorizationSuccessPacket(BaseModel):
    """The packet sent by the server to acknowledge the authorization."""

    success: Literal[True] = Field(
        description="indicates that the authorization was successful"
    )


class AuthorizationFailurePacket(BaseModel):
    """The packet sent by the server to acknowledge the authorization."""

    success: Literal[False] = Field(
        description="indicates that the authorization was unsuccessful"
    )
    error_category: int = Field(
        description="the category of the error, a number interpreted like an http status code"
    )
    error_type: Literal[
        "account_not_found", "progress_bar_not_found", "invalid_packet"
    ] = Field(description="the type of error that occurred during authorization")
    error_message: str = Field(description="the human readable error message")


class UpdatePacketData(BaseModel):
    overall_eta_seconds: float = Field(
        description="the estimated total progress bar duration in seconds"
    )
    remaining_eta_seconds: float = Field(
        description="the estimated time in seconds remaining for the progress bar to finish, may be negative, whihc means it's taking longer than expected"
    )
    step_name: str = Field(description="the name of the current step")
    step_overall_eta_seconds: float = Field(
        description="the estimated time in seconds in total for the current step to finish"
    )
    step_remaining_eta_seconds: float = Field(
        description="the estimated time in seconds remaining for the current step to finish, may be negative, whihc means it's taking longer than expected"
    )


class UpdatePacket(BaseModel):
    """The packet sent by the server to update the client."""

    done: bool = Field(description="whether the trace is done")
    type: Literal["update"] = Field(description="the type of the packet")
    data: UpdatePacketData = Field(description="the data of the packet")


@dataclass
class TraceInfo:
    created_at: float
    last_updated_at: float
    current_step: int
    done: bool


@dataclass
class TraceStepInfo:
    step_name: str
    iteration: Optional[int]
    iterations: Optional[int]
    started_at: float
    finished_at: Optional[float]


@dataclass
class BarInfo:
    user_sub: str
    uid: str
    name: str
    version: int
    default_one_off_technique: str
    default_one_off_percentile: Optional[float]
    bootstrapped: bool


@dataclass
class BarStepInfo:
    uid: str
    name: str
    iterated: bool
    technique: str
    percentile: float
    bootstrapped: bool


class BarEtaStepItem(BaseModel):
    step_name: str = Field(description="the name of the step")
    iterated: bool = Field(description="whether the step is iterated")
    technique: str = Field(description="the technique used to calculate the eta")
    percentile: Optional[float] = Field(
        description="the percentile if the technique is percentile"
    )
    eta_a: float = Field(
        description="the first variable in the fit; for all techniques except best_fit.linear this is the only variable required - for example, if the technique is arithmetic mean, this is the arithmetic mean. for best_fit.linear, this is the slope of the fit"
    )
    eta_b: Optional[float] = Field(
        description="the second variable of the fit; unused except for best_fit.linear, where this is the intercept of the fit"
    )


class BarEta(BaseModel):
    technique: str = Field(
        description="the technique used to calculate the overall eta, this is a one-off technique"
    )
    percentile: Optional[float] = Field(
        description="the percentile if the technique is percentile"
    )
    eta: float = Field(description="the eta calculated using the technique")
    steps: List[BarEtaStepItem] = Field(
        description="the steps of the progress bar in order"
    )
    version: int = Field(
        description="the version of the progress bar at the time of calculation"
    )


class RedisPubSubMessage(TypedDict):
    type: Literal["message", "pong"]
    pattern: Optional[bytes]
    channel: Optional[bytes]
    data: bytes


@router.websocket("/")
async def watch_progress_bar_trace(websocket: WebSocket):
    """see /docs/watch_progress_bar_trace.md"""
    try:
        await asyncio.wait_for(websocket.accept(), timeout=CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        await websocket.close(code=1008)
        return

    try:
        message_raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=AUTH_TIMEOUT
        )
    except asyncio.TimeoutError:
        await websocket.close(code=1008)
        return

    try:
        message = AuthorizationPacket.parse_raw(message_raw)
    except ValidationError as e:
        try:
            await asyncio.wait_for(
                websocket.send_text(
                    AuthorizationFailurePacket(
                        success=False,
                        error_category=400,
                        error_type="invalid_packet",
                        error_message=f"The packet sent by the client was invalid: {e.json()}",
                    ).json()
                ),
                timeout=SEND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            pass
        await websocket.close(code=1008)
        return

    async with Itgs() as itgs:
        conn = await itgs.conn()
        cursor = conn.cursor("none")
        response = await cursor.execute(
            """
            SELECT
                users.sub,
                progress_bars.uid,
                progress_bars.name,
                progress_bars.version,
                progress_bar_steps.one_off_technique,
                progress_bar_steps.one_off_percentile
            FROM progress_bars
            JOIN users ON users.id = progress_bars.user_id
            JOIN progress_bar_steps ON progress_bar_steps.progress_bar_id = progress_bars.id
            WHERE
                users.sub = ?
                AND progress_bars.name = ?
                AND progress_bar_steps.position = 0
            """,
            (message.sub, message.progress_bar_name),
        )
        steps_info: List[BarStepInfo] = []
        if response.results:
            bar_info = BarInfo(
                user_sub=response.results[0][0],
                uid=response.results[0][1],
                name=response.results[0][2],
                version=response.results[0][3],
                default_one_off_technique=response.results[0][4],
                default_one_off_percentile=response.results[0][5],
                bootstrapped=False,
            )
            response = await cursor.execute(
                """
                SELECT
                    progress_bar_steps.uid,
                    progress_bar_steps.name,
                    progress_bar_steps.iterated,
                    progress_bar_steps.one_off_technique,
                    progress_bar_steps.one_off_percentile,
                    progress_bar_steps.iterated_technique,
                    progress_bar_steps.iterated_percentile
                FROM progress_bar_steps
                WHERE 
                    progress_bar_steps.progress_bar_uid = ?
                    AND progress_bar_steps.position != 0
                ORDER BY progress_bar_steps.position ASC
                """,
                (bar_info.uid,),
            )
            steps_info = [
                BarStepInfo(
                    uid=step[0],
                    name=step[1],
                    iterated=bool(step[2]),
                    technique=step[3] if not step[2] else step[5],
                    percentile=step[4] if not step[2] else step[6],
                    bootstrapped=False,
                )
                for step in response.results
            ]
        else:
            bar_info = BarInfo(
                user_sub=message.sub,
                uid="ep_pb_" + secrets.token_urlsafe(8),
                name=message.progress_bar_name,
                version=0,
                default_one_off_technique="percentile",
                default_one_off_percentile=75,
                bootstrapped=True,
            )
        eta_info = await get_trace_eta(itgs, bar_info, steps_info)
        if eta_info.version != bar_info.version:
            return await websocket.close(code=1013)
        redis = await itgs.redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(
            f"ps:trace:{bar_info.user_sub}:{bar_info.name}:{message.progress_bar_trace_uid}"
        )
        websocket_ready_at = time.time()

        while True:
            redis_info: Optional[
                Tuple[TraceInfo, List[TraceStepInfo]]
            ] = await redis.transaction(
                (
                    lambda pipe: get_info_from_redis(
                        pipe, bar_info, message.progress_bar_trace_uid
                    )
                ),
                redis_trace_key(bar_info, message.progress_bar_trace_uid),
                value_from_callable=True,
            )
            try:
                if bar_info.bootstrapped:
                    if redis_info is None:
                        await asyncio.wait_for(
                            websocket.send_text(
                                UpdatePacket(
                                    done=False,
                                    type="update",
                                    data=UpdatePacketData(
                                        overall_eta_seconds=0.0,
                                        remaining_eta_seconds=0.0,
                                        step_name="preparing",
                                        step_overall_eta_seconds=0.0,
                                        step_remaining_eta_seconds=0.0,
                                    ),
                                ).json()
                            ),
                            timeout=SEND_TIMEOUT,
                        )
                    else:
                        now = time.time()
                        await asyncio.wait_for(
                            websocket.send_text(
                                UpdatePacket(
                                    done=redis_info[0].done,
                                    type="update",
                                    data=UpdatePacketData(
                                        overall_eta_seconds=0.0,
                                        remaining_eta_seconds=redis_info[0].created_at
                                        - now,
                                        step_name=redis_info[1][-1].step_name,
                                        step_overall_eta_seconds=0.0,
                                        step_remaining_eta_seconds=redis_info[1][
                                            -1
                                        ].started_at
                                        - now,
                                    ),
                                ).json()
                            ),
                            timeout=SEND_TIMEOUT,
                        )
                        if redis_info[0].done:
                            break
                else:
                    if redis_info is None:
                        await asyncio.wait_for(
                            websocket.send_text(
                                UpdatePacket(
                                    done=False,
                                    type="update",
                                    data=UpdatePacketData(
                                        overall_eta_seconds=eta_info.eta,
                                        remaining_eta_seconds=eta_info.eta,
                                        step_name=steps_info[0].name,
                                        step_overall_eta_seconds=eta_info.steps[
                                            0
                                        ].eta_a,
                                        step_remaining_eta_seconds=eta_info.steps[
                                            0
                                        ].eta_a,
                                    ),
                                ).json()
                            ),
                            timeout=SEND_TIMEOUT,
                        )
                    else:
                        if len(redis_info[1]) > len(steps_info):
                            bar_info.version = -1
                            bar_info.bootstrapped = True
                            steps_info = []
                            continue
                        for expected_step, actual_step in zip(
                            steps_info, redis_info[1]
                        ):
                            if (
                                expected_step.name != actual_step.step_name
                                or expected_step.iterated
                                != (actual_step.iterations is not None)
                            ):
                                bar_info.version = -1
                                bar_info.bootstrapped = True
                                steps_info = []
                                break
                        if bar_info.bootstrapped:
                            continue
                        now = time.time()
                        step_eta: float = 0.0
                        if redis_info[1][-1].iterations is None:
                            step_eta = eta_info.steps[0].eta_a
                        elif (
                            steps_info[len(redis_info[1]) - 1].technique
                            == "best_fit.linear"
                        ):
                            step_eta_info = eta_info.steps[len(redis_info[1]) - 1]
                            step_eta = (
                                step_eta_info.eta_a
                                + step_eta_info.eta_b * redis_info[1][-1].iterations
                            )
                        else:
                            step_eta = (
                                eta_info.steps[len(redis_info[1]) - 1].eta_a
                                * redis_info[1][-1].iterations
                            )
                        await asyncio.wait_for(
                            websocket.send_text(
                                UpdatePacket(
                                    done=redis_info[0].done,
                                    type="update",
                                    data=UpdatePacketData(
                                        overall_eta_seconds=eta_info.eta,
                                        remaining_eta_seconds=eta_info.eta
                                        - (now - redis_info[0].created_at),
                                        step_name=redis_info[1][-1].step_name,
                                        step_overall_eta_seconds=step_eta,
                                        step_remaining_eta_seconds=step_eta
                                        - (now - redis_info[1][-1].started_at),
                                    ),
                                ).json()
                            ),
                            timeout=SEND_TIMEOUT,
                        )
                        if redis_info[0].done:
                            break
            except asyncio.TimeoutError:
                break

            if redis_info is None:
                timeout = TRACE_EXISTS_TIMEOUT
            else:
                timeout = SERVER_IDLE_TIMEOUT

            timeout_remaining = timeout - (time.time() - websocket_ready_at)
            if timeout_remaining <= 0:
                break

            try:
                await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True),
                    timeout=timeout_remaining,
                )
            except asyncio.TimeoutError:
                break

        try:
            await asyncio.wait_for(websocket.close(), timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            pass


def redis_trace_key(bar_info: BarInfo, trace_uid: str) -> str:
    return f"trace:{bar_info.user_sub}:{bar_info.name}:{trace_uid}"


async def get_info_from_redis(
    redis: Redis, bar_info: BarInfo, trace_uid: str
) -> Optional[Tuple[TraceInfo, List[TraceStepInfo]]]:
    trace_info_raw = await redis.hmget(
        redis_trace_key(bar_info, trace_uid),
        "created_at",
        "last_updated_at",
        "current_step",
        "done",
    )
    if not trace_info_raw[0]:
        return None
    trace_info = TraceInfo(
        created_at=float(trace_info_raw[0]),
        last_updated_at=float(trace_info_raw[1]),
        current_step=int(trace_info_raw[2]),
        done=bool(int(trace_info_raw[3])),
    )
    steps_info: List[TraceStepInfo] = []
    for step in range(1, trace_info.current_step + 1):
        step_info_raw = await redis.hmget(
            f"trace:{bar_info.user_sub}:{bar_info.name}:{trace_uid}:step:{step}",
            "step_name",
            "iteration",
            "iterations",
            "started_at",
            "finished_at",
        )
        step_info = TraceStepInfo(
            step_name=step_info_raw[0],
            iteration=int(step_info_raw[1]),
            iterations=int(step_info_raw[2]),
            started_at=float(step_info_raw[3]),
            finished_at=float(step_info_raw[4]) if step_info_raw[4] else None,
        )
        if step_info.iterations == 0:
            step_info.iterations = None
            step_info.iteration = None
        steps_info.append(step_info)

    return trace_info, steps_info


async def get_cached_trace_eta(
    itgs: Itgs, bar_info: BarInfo, steps: List[BarStepInfo]
) -> Optional[BarEta]:
    """
    Gets the eta for the given non-bootstrapped progress bar if it's available in redis, otherwise,
    returns None.

    Args:
        itgs (Itgs): The Itgs instance to use.
        bar_info (BarInfo): The progress bar info to fetch the stats for.
        steps (List[BarStepInfo]): The steps for the given non-bootstrapped progress bar.

    Returns:
        BarEta, None: the eta for the progress bar, if available, otherwise None
    """
    redis = await itgs.redis()
    root_key = f"stats:{bar_info.user_sub}:{bar_info.name}:{bar_info.version}:{bar_info.default_one_off_technique}"
    if bar_info.default_one_off_technique == "percentile":
        root_key += f"_{bar_info.default_one_off_percentile}"
    step_keys: List[str] = []
    for idx, step in enumerate(steps):
        step_key = f"stats:{bar_info.user_sub}:{bar_info.name}:{bar_info.version}:{idx + 1}:{step.technique}"
        if step.technique == "percentile":
            step_key += f"_{step.percentile}"
        step_keys.append(step_key)

    async def real_get(pipe: Redis) -> Optional[BarEta]:
        root_eta = await pipe.get(root_key)
        if root_eta is None:
            return None
        result_steps: List[BarEtaStepItem] = []
        for step, step_key in zip(steps, step_keys):
            a, b = await pipe.hmget(step_key, "a", "b")
            if a is None:
                return None
            result_steps.append(
                BarEtaStepItem(
                    step_name=step.name,
                    iterated=step.iterated,
                    technique=step.technique,
                    percentile=step.percentile,
                    eta_a=float(a),
                    eta_b=float(b) if b is not None else None,
                )
            )
        return BarEta(
            technique=bar_info.default_one_off_technique,
            percentile=bar_info.default_one_off_percentile,
            eta=float(root_eta),
            steps=result_steps,
            version=bar_info.version,
        )

    return await redis.transaction(
        real_get, root_key, *step_keys, value_from_callable=True
    )


async def get_trace_eta(
    itgs: Itgs, bar_info: BarInfo, steps: List[BarStepInfo]
) -> BarEta:
    """
    Gets the estimated time to completion for the entire trace - if cached, this returns the cached values,
    otherwise it uses the jobs server to calculate the ETA and cache it for future use.

    Args:
        itgs (Itgs): The Itgs instance to use.
        bar_info (BarInfo): The progress bar info to fetch the stats for.
        steps (List[BarStepInfo]): The steps for the progress bar.

    Returns:
        BarEta: The ETA for the progress bar.

    Raises:
        asyncio.TimeoutError: If the ETA could not be fetched within the timeout.
    """
    if bar_info.bootstrapped:
        return BarEta(technique="percentile_75", eta=0.0, steps=[], version=-1)
    cached_eta = await get_cached_trace_eta(itgs, bar_info, steps)
    if cached_eta is not None:
        return cached_eta
    redis = await itgs.redis()
    jobs = await itgs.jobs()
    job_uid = secrets.token_urlsafe(16)
    async with redis.pubsub() as pubsub:
        pubsub: PubSub
        await pubsub.subscribe(f"ps:job:{job_uid}")
        await jobs.enqueue(
            "runners.calc_pbar_eta",
            user_sub=bar_info.user_sub,
            pbar_name=bar_info.name,
            job_uid=job_uid,
        )
        started_at = time.time()
        while True:
            time_remaining = (started_at + JOB_STATS_TIMEOUT) - time.time()
            if time_remaining <= 0:
                raise asyncio.TimeoutError()
            message: Optional[RedisPubSubMessage] = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=JOB_STATS_TIMEOUT
            )
            if message is not None or message["type"] != "message":
                break
        await pubsub.unsubscribe(f"ps:job:{job_uid}")
    return BarEta.parse_raw(message["data"])
