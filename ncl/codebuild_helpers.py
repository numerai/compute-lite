import boto3
import botocore.config
import collections
import sys
import time


def start_build(repo_name, cb_project_name):
    args = {"projectName": cb_project_name}
    session = boto3.session.Session()
    client = session.client("codebuild")

    response = client.start_build(**args)
    return response["build"]["id"]


def wait_for_build(id, poll_seconds=10):
    session = boto3.session.Session()
    client = session.client("codebuild")
    status = client.batch_get_builds(ids=[id])
    first = True
    while status["builds"][0]["buildStatus"] == "IN_PROGRESS":
        if not first:
            print(".", end="")
            sys.stdout.flush()
        first = False
        time.sleep(poll_seconds)
        status = client.batch_get_builds(ids=[id])
    print()
    print(f"Build complete, status = {status['builds'][0]['buildStatus']}")
    print(f"Logs at {status['builds'][0]['logs']['deepLink']}")


# Position is a tuple that includes the last read timestamp and the number of items that were read
# at that time. This is used to figure out which event to start with on the next read.
Position = collections.namedtuple("Position", ["timestamp", "skip"])


class LogState(object):
    STARTING = 1
    WAIT_IN_PROGRESS = 2
    TAILING = 3
    JOB_COMPLETE = 4
    COMPLETE = 5


def log_stream(client, log_group, stream_name, position):
    start_time, skip = position
    next_token = None

    event_count = 1
    while event_count > 0:
        if next_token is not None:
            token_arg = {"nextToken": next_token}
        else:
            token_arg = {}

        response = client.get_log_events(
            logGroupName=log_group,
            logStreamName=stream_name,
            startTime=start_time,
            startFromHead=True,
            **token_arg,
        )
        next_token = response["nextForwardToken"]
        events = response["events"]
        event_count = len(events)
        if event_count > skip:
            events = events[skip:]
            skip = 0
        else:
            skip = skip - event_count
            events = []
        for ev in events:
            ts, count = position
            if ev["timestamp"] == ts:
                position = Position(timestamp=ts, skip=count + 1)
            else:
                position = Position(timestamp=ev["timestamp"], skip=1)
            yield ev, position


def logs_for_build(
        build_id, wait=False, poll=10, session=None
):  # noqa: C901 - suppress complexity warning for this method

    codebuild = boto3.client("codebuild")
    description = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
    status = description["buildStatus"]

    log_group = description["logs"].get("groupName")
    stream_name = description["logs"].get("streamName")  # The list of log streams
    position = Position(
        timestamp=0, skip=0
    )  # The current position in each stream, map of stream name -> position

    # Increase retries allowed (from default of 4), as we don't want waiting for a build
    # to be interrupted by a transient exception.
    config = botocore.config.Config(retries={"max_attempts": 15})
    client = boto3.client("logs", config=config)

    job_already_completed = False if status == "IN_PROGRESS" else True

    state = (
        LogState.STARTING if wait and not job_already_completed else LogState.COMPLETE
    )
    dot = True

    while state == LogState.STARTING and log_group == None:
        time.sleep(poll)
        description = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        log_group = description["logs"].get("groupName")
        stream_name = description["logs"].get("streamName")

    if state == LogState.STARTING:
        state = LogState.TAILING

    # The loop below implements a state machine that alternates between checking the build status and
    # reading whatever is available in the logs at this point. Note, that if we were called with
    # wait == False, we never check the job status.
    #
    # If wait == TRUE and job is not completed, the initial state is STARTING
    # If wait == FALSE, the initial state is COMPLETE (doesn't matter if the job really is complete).
    #
    # The state table:
    #
    # STATE               ACTIONS                        CONDITION               NEW STATE
    # ----------------    ----------------               -------------------     ----------------
    # STARTING            Pause, Get Status              Valid LogStream Arn     TAILING
    #                                                    Else                    STARTING
    # TAILING             Read logs, Pause, Get status   Job complete            JOB_COMPLETE
    #                                                    Else                    TAILING
    # JOB_COMPLETE        Read logs, Pause               Any                     COMPLETE
    # COMPLETE            Read logs, Exit                                        N/A
    #
    # Notes:
    # - The JOB_COMPLETE state forces us to do an extra pause and read any items that got to Cloudwatch after
    #   the build was marked complete.
    last_describe_job_call = time.time()
    dot_printed = False
    while True:
        for event, position in log_stream(client, log_group, stream_name, position):
            print(event["message"].rstrip())
            if dot:
                dot = False
                if dot_printed:
                    print()
        if state == LogState.COMPLETE:
            break

        time.sleep(poll)
        if dot:
            print(".", end="")
            sys.stdout.flush()
            dot_printed = True
        if state == LogState.JOB_COMPLETE:
            state = LogState.COMPLETE
        elif time.time() - last_describe_job_call >= 30:
            description = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
            status = description["buildStatus"]

            last_describe_job_call = time.time()

            status = description["buildStatus"]

            if status != "IN_PROGRESS":
                print()
                state = LogState.JOB_COMPLETE

    if wait:
        if dot:
            print()
