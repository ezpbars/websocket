# /api/2/progress_bars/traces/

watch for changes in the in progress progress bar

## initial handshake

the client should pass a json message in the following format immediately after
opening the connection:

```json
{
    "sub": "string",
    "progress_bar_name": "string",
    "progress_bar_trace_uid": "string"
}
```

where:
- `sub (str)`: the identifier for the account the progress bar belongs to
- `progress_bar_name (str)`: the name of the progress bar
- `progress_bar_trace_uid (str)`: the uid of the progress bar trace to watch

if the authentication fails, the format of the response will be:

```json
{
    "success": false,
    "error_category": 0,
    "error_type": "string",
    "error_message": "string"
}
```

where:
- `success (False)`: used to indicate that the authentication failed
- `error_category (int)`: the category of the error, a number interpreted like an http status code
- `error_type ("account_not_found", "progress_bar_not_found", "invalid_packet")`: the type of the error
- `error_message (str)`: a human readable message describing the error

and then the connection will be terminated.

otherwise, if the authentication succeeds, the next message will be in the following format:

```json
{
    "success": true
}
```

where:
- `success (True)`: used to indicate that the authentication succeeded

and then the client should not send any more messages unless otherwise indicated
and will receive messages in the following format:

```json
{
    "done": false,
    "type": "update",
    "data": {
        "overall_eta_seconds": 0.0,
        "remaining_eta_seconds": 0.0,
        "step_name": "string",
        "step_overall_eta_seconds": 0.0,
        "step_remaining_eta_seconds": 0.0
    }
}
```

where:
- `done (bool)`: used to indicate that the progress bar has finished. If true,
  the client should close the connection, otherwise it should continue to listen
  for messages.
- `type ("update")`: the type of the message. Currently, the only type is "update".
- `overall_eta_seconds (float)`: the estimated time in seconds in total for the
  progress bar to finish
- `remaining_eta_seconds (float)`: the estimated time in seconds remaining for
  the progress bar to finish, may be negative, which means it's taking longer
  than expected
- `step_name (str)`: the name of the current step
- `step_overall_eta_seconds (float)`: the estimated time in seconds in total for
  the current step to finish
- `step_remaining_eta_seconds (float)`: the estimated time in seconds remaining
  for the current step to finish, may be negative, which means it's taking longer
  than expected

if the connection is lost, the client should reconnect with the following delays
based on how many times it has failed in the last minute, including this one:

1. 0 seconds
2. 0 seconds
3. 1 seconds
4. 5 seconds
5. 15 seconds

if the connection is lost more than 5 times in the last minute, the client should
continue to reconnect with a delay of 15 seconds and begin polling the result.
