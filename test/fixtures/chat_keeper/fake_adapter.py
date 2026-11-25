"""A stand-in ACP adapter for the chat keeper tests: newline-delimited JSON-RPC on stdio.

Requests are answered concurrently, as claude-agent-acp answers them, so a request
sent while a prompt runs is answered while the prompt is still running.

* ``session/new`` answers a session ``S1`` in mode ``default`` (with ``models``,
  on model ``m1`` too); ``session/set_mode`` and ``session/set_model`` answer ``{}``
  and send no update, as claude-agent-acp does. ``session/set_config_option``
  answers the written option (or ``answer_value``) as the session's ``configOptions``
  and sends no update.
* ``echo`` answers its params; with ``gate`` (a FIFO the test made) it first waits
  until the test writes to it, then writes ``<gate>.done`` after the answer is out.
* ``session/prompt`` writes ``before`` chunks, then (with ``ask``) asks
  ``session/request_permission`` (id 900), after a ``tool_call`` it names when
  ``tool`` is set, and writes the answer it got as a chunk,
  then waits for ``gate`` (when given), writes ``after`` chunks and answers.
* ``exit`` exits with ``code``.
* ``ignore_term`` ignores SIGTERM from then on, so only a SIGKILL ends it.

A gate is a FIFO: opening it for reading blocks until the test opens it to write,
so the fake waits on the test's signal itself, never on a duration.
"""

import json
import os
import queue
import signal
import sys
import threading

_lock = threading.Lock()
_answers = {}


def _out(obj):
    with _lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _chunk(text):
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    _out(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "S1", "update": update},
        }
    )


def _wait_for(path):
    with open(path, "rb") as gate:
        gate.read()


def _prompt(req_id, params):
    for n in range(int(params.get("before", 0))):
        _chunk(f"b{n}")
    if params.get("ask"):
        answer = queue.Queue()
        _answers[900] = answer
        ask = {"sessionId": "S1"}
        if params.get("tool"):
            update = {"sessionUpdate": "tool_call", "toolCallId": "T1", "rawInput": {"p": 1}}
            _out(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": "S1", "update": update},
                }
            )
            ask["toolCall"] = {"toolCallId": "T1"}
        _out({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": ask})
        _chunk("answered:" + json.dumps(answer.get(), sort_keys=True))
    if params.get("gate"):
        _wait_for(params["gate"])
    for n in range(int(params.get("after", 0))):
        _chunk(f"a{n}")
    _out({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})


def _echo(req_id, params):
    gate = params.get("gate")
    if gate:
        _wait_for(gate)
    _out({"jsonrpc": "2.0", "id": req_id, "result": params})
    if gate:
        with open(gate + ".done", "w") as handle:
            handle.write("done")


def main():
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        method, req_id = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method is None and req_id in _answers:
            _answers.pop(req_id).put(msg.get("result"))
        elif method == "session/new":
            result = {"sessionId": "S1", "modes": {"currentModeId": "default"}}
            if params.get("models"):
                result["models"] = {"currentModelId": "m1", "availableModels": []}
            _out({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method in ("session/set_mode", "session/set_model"):
            _out({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "session/set_config_option":
            # ``answer_value`` stands in for an adapter that resolves the written
            # value to another spelling of the model it runs.
            value = params.get("answer_value", params.get("value"))
            option = {"id": params.get("configId"), "currentValue": value}
            _out({"jsonrpc": "2.0", "id": req_id, "result": {"configOptions": [option]}})
        elif method == "umask":
            mask = os.umask(0)
            os.umask(mask)
            _out({"jsonrpc": "2.0", "id": req_id, "result": {"umask": mask}})
        elif method == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _out({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "echo":
            threading.Thread(target=_echo, args=(req_id, params), daemon=True).start()
        elif method == "session/prompt":
            threading.Thread(target=_prompt, args=(req_id, params), daemon=True).start()
        elif method == "exit":
            sys.stdout.flush()
            os._exit(int(params.get("code", 0)))


if __name__ == "__main__":
    main()
