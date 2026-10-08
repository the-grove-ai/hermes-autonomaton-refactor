#!/usr/bin/env python3
"""harness_run.py — work a goal's queue with a SCRIPTED operator and LIVE models.

METHODOLOGY (read this before quoting any number from a harness run)

What is real: every turn goes through the running gateway's API surface and
the full five-stage pipeline. The models, the routing, the kegs, Jidoka's
flags, Kaizen's proposals, the ladder, the time budgets and every record are
exactly what a person at the keyboard produces. Cost is what the provider
charged.

What is scripted: the operator. This script sends the operator's messages
(the start phrase, a confirmation, a revised value, "ship it", Yes on a
question card) and signs Kaizen's proposals through the portal's own approve
action. Jim Calhoun authorized that signing for development and testing on
2026-10-07; it is not a general permission.

How the scripted operator decides: from the operator's OWN PAST RULINGS, read
off the goal's decision log (``--build-policy``). For each item it knows what
the operator finally confirmed or revised it to in earlier runs, by majority
across the runs it was told to use. A proposal that matches that ruling is
confirmed; one that does not is revised to it. No answer key is read, here or
anywhere. An item with no past ruling stops the run: nothing is guessed.

What varies between runs (``--seed``): how a confirmation is given (the
button's message, the word, or "ship it" once it has been earned) and short
pauses. The rulings never vary: a random correction would teach a wrong rule.

How a run is marked: its run record is labelled "harness-driven" with the
seed, and its turns carry surface ``api_server``. Records made by this script
must never be presented as a person's session.

Run it on the node, as the user that owns the node's home:

    scripts/harness_run.py --build-policy --runs 12-17 --out ~/harness/policy.json
    scripts/harness_run.py --policy ~/harness/policy.json --reset --seed 1 \
        --months 2 --checkpoint before-month-3
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

BASE = os.environ.get("HARNESS_GATEWAY", "http://127.0.0.1:8642")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the policy: the operator's own past rulings ───────────────────────


def build_policy(goal: str, runs: set) -> dict:
    """Each item's ruling from the operator's past decisions in ``runs``: the
    output they confirmed or revised it to, by majority (ties: the later
    run). Items only ever accepted from a keg, never ruled on, are listed
    apart: they are a fallback, marked as such when used."""
    from grove.decision_work import DecisionLog

    log = DecisionLog(goal)
    run_of, proposed = {}, {}
    ruled, accepted = {}, {}
    for r in log.records():
        if r.get("kind") == "run_started":
            run_of[r["run_id"]] = int(r["run_number"])
        elif r.get("kind") == "proposed":
            proposed[r["id"]] = r
        elif r.get("kind") == "decided" and r.get("ref") in proposed:
            n = run_of.get(r.get("run_id"))
            if n not in runs:
                continue
            item, out = r["item_id"], json.dumps(r.get("output") or {}, sort_keys=True)
            if r.get("decision") in ("confirm", "correct"):
                ruled.setdefault(item, []).append((n, out))
            elif r.get("decision") == "accepted":
                accepted.setdefault(item, []).append((n, out))

    def pick(votes):
        tally = Counter(out for _n, out in votes)
        top = max(tally.values())
        best = [out for out, c in tally.items() if c == top]
        latest = max((n, out) for n, out in votes if out in best)[1]
        return json.loads(latest), {json.loads(o).__str__(): c for o, c in tally.items()}

    items = {}
    for item, votes in ruled.items():
        out, tally = pick(votes)
        items[item] = {"output": out, "from": "ruled", "votes": tally}
    for item, votes in accepted.items():
        if item not in items:
            out, tally = pick(votes)
            items[item] = {"output": out, "from": "keg_accepted_only", "votes": tally}
    return {"goal": goal, "runs": sorted(runs), "built_at": _now(),
            "method": "the operator's own past rulings, by majority across the runs named",
            "items": items}


# ── the gateway ───────────────────────────────────────────────────────


class Gateway:
    def __init__(self, key: str):
        self.key = key

    def say(self, session: str, text: str, timeout: int = 240) -> str:
        body = json.dumps({"model": "hermes", "messages": [{"role": "user", "content": text}]})
        req = urllib.request.Request(
            BASE + "/v1/chat/completions", data=body.encode("utf-8"),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                     "X-Hermes-Session-Id": session})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or [{}]
        return str((choices[0].get("message") or {}).get("content") or "")

    def portal(self, path: str) -> int:
        req = urllib.request.Request(BASE + path, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code


# ── one run ───────────────────────────────────────────────────────────


class Harness:
    def __init__(self, args, policy: dict):
        from grove import decision_work as dw

        self.dw, self.args, self.goal = dw, args, policy["goal"]
        self.policy = policy["items"]
        self.rng = random.Random(args.seed)
        env = Path(os.environ.get("GROVE_HOME", str(Path.home() / ".grove"))) / ".env"
        key = next((ln.split("=", 1)[1].strip().strip("\"'") for ln in env.read_text().splitlines()
                    if ln.startswith("API_SERVER_KEY=")), "")
        if not key:
            raise SystemExit("no API_SERVER_KEY in the node's .env")
        self.gw = Gateway(key)
        self.session = f"harness-s{args.seed}-{int(time.time())}"
        self.log_path = Path(args.transcript or f"/tmp/harness-{self.session}.jsonl")
        self.turns = 0
        self.inferred = []

    # -- state, read in-process (the driver decides nothing from reply text
    #    except how to answer a question or a pause)
    def work(self):
        return self.dw.DecisionWork(self.dw.config_for_goal(self.goal))

    def state(self) -> dict:
        from grove import adaptation, reissue
        from grove.eval.proposal_queue import read_all

        work = self.work()
        recs = work.log.run_records()
        return {
            "decided": sum(1 for r in recs if r.get("kind") == "decided"),
            "proposed": sum(1 for r in recs if r.get("kind") == "proposed"),
            "pending": work.pending(),
            "next": getattr(work.next_item(), "stem", None),
            "held": bool(reissue.held(self.session)),
            "keg_proposals": [p for p in read_all()
                              if ((p.payload or {}).get("keg") or {}).get("dock_goal") == self.goal],
            "alias_questions": adaptation.pending(self.goal),
            "stage": self.dw.next_backlog_stage(work.config),
            "work": work,
        }

    def note(self, **entry) -> None:
        entry = {"at": _now(), "session": self.session, **entry}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        if not self.args.quiet:
            said = str(entry.get("say") or entry.get("action") or "")
            print(f"  {entry['at'][11:19]} {said[:46]:<46} | {str(entry.get('reply') or '')[:96]!r}")

    def say(self, text: str, why: str) -> str:
        self.turns += 1
        if self.turns > self.args.max_turns:
            raise SystemExit(f"stopped: more than {self.args.max_turns} turns")
        time.sleep(self.rng.uniform(0.3, 1.5))
        reply = self.gw.say(self.session, text)
        self.note(say=text, why=why, reply=reply.replace("\n", " | ")[:400])
        return self.after_turn(reply)

    def after_turn(self, reply: str) -> str:
        """What a chat surface does for the operator after each turn, done
        here because the API surface answers a request and cannot push:

          * a request the ladder armed to be retried one tier up is re-issued,
            pinned to that tier, exactly as the gateway re-issues it in chat;
          * a request armed to bring the next item is dropped (this loop asks
            for the next item itself);
          * cards and buttons offered for the turn are taken, as a chat
            surface takes them when it shows them.
        """
        from grove import reissue

        reissue.take_cards(self.session)
        reissue.take_actions(self.session)
        for _ in range(3):                       # at most T1 -> T2 -> T3
            armed = reissue.take(self.session)
            if not armed or not armed.get("tier"):
                break
            request = str(armed.get("request") or "")
            if not request:
                break
            reissue.arm_tier(self.session, str(armed["tier"]), attempts=armed.get("attempts"),
                             andon_id=armed.get("andon_id"), request=request)
            self.turns += 1
            reply = self.gw.say(self.session, request)
            self.note(say=request, why=f"the ladder's retry at {armed['tier']}",
                      reply=reply.replace("\n", " | ")[:400])
            reissue.take_cards(self.session)
            reissue.take_actions(self.session)
        return reply

    def ruling(self, item_id: str) -> dict:
        entry = self.policy.get(item_id)
        if entry is None:
            raise SystemExit(f"stopped: no past ruling for {item_id}; nothing is guessed")
        if entry["from"] != "ruled" and item_id not in self.inferred:
            self.inferred.append(item_id)
            self.note(action="ruling_inferred", item=item_id,
                      note="never ruled on by the operator; using what a keg decided and "
                           "the operator let stand")
        return entry["output"]

    def confirm_words(self, state: dict, pending: dict) -> str:
        from grove import adaptation

        learned = "ship it" in (adaptation.load(self.goal).get("confirm") or [])
        by_model = not pending.get("keg")
        if not learned and by_model and self.args.earn_phrase:
            return "ship it"                       # earning it: a model has to read it
        button = self.dw.button_message("confirm", pending["item_id"])
        options = [button, button, "confirm"] + (["ship it", "ship it"] if learned else [])
        return self.rng.choice(options)

    def run(self) -> dict:
        ws = self.work().config.work_session
        months_done, stuck, last_sig, reply = 0, 0, None, ""
        self.say(ws.start[0], "open the work")
        while True:
            st = self.state()
            sig = (st["decided"], st["proposed"], (st["pending"] or {}).get("item_id"),
                   len(st["keg_proposals"]), len(st["alias_questions"]), st["held"])
            stuck = stuck + 1 if sig == last_sig else 0
            last_sig = sig
            if stuck >= 6:
                raise SystemExit(f"stopped: no progress after 6 turns (state {sig})")
            work = st["work"]
            if st["alias_questions"]:
                q = st["alias_questions"][0]
                short = q.proposal_id.split(":")[-1][:12]
                reply = self.say(self.dw.alias_message("yes", short), "approve the phrase (Yes)")
                continue
            if st["keg_proposals"]:
                for p in st["keg_proposals"]:
                    short = p.proposal_id.split(":")[-1][:12]
                    time.sleep(self.rng.uniform(2.0, 5.0))      # the operator reads the card
                    # The portal's approve action takes the proposal's full id.
                    code = self.gw.portal("/portal/actions/proposals/"
                                          + urllib.parse.quote(p.proposal_id, safe="") + "/approve")
                    version = ((p.payload or {}).get("keg") or {}).get("version")
                    self.note(action=f"sign keg v{version}", reply=f"portal approve http {code}")
                    if code != 200:
                        raise SystemExit(f"stopped: signing v{version} returned http {code}")
                continue
            pending = st["pending"]
            if pending is not None:
                want = self.ruling(pending["item_id"])
                got = {k: str(v) for k, v in (pending.get("output") or {}).items()}
                if got == {k: str(v) for k, v in want.items()}:
                    reply = self.say(self.confirm_words(st, pending), "matches the past ruling")
                else:
                    value = next(iter(want.values()))
                    reply = self.say(str(value), f"past ruling differs: revise to {value}")
                continue
            if st["next"] is None:
                months_done += 1
                self.note(action=f"month {months_done} complete", reply=reply[:200])
                if self.args.checkpoint and months_done == self.args.months and not st["held"]:
                    self.save_checkpoint(self.args.checkpoint)
                for part in filter(None, self.args.checkpoint_at.split(",")):
                    month, _, name = part.partition(":")
                    if int(month) == months_done and not st["held"]:
                        self.save_checkpoint(name)
                if months_done >= self.args.months or st["stage"] is None:
                    break
                code = self.gw.portal(f"/portal/actions/demo/{self.goal}/release")
                self.note(action=f"release {st['stage']['label']}", reply=f"portal http {code}")
                reply = self.say(ws.batch[0] if ws.batch else work.config.keg.request,
                                 "work the released backlog")
                continue
            # Nothing waiting, items left. Ask for the next one; or, when the
            # last reply was a question or a stray pause, answer that.
            if stuck and reply.strip().lower().startswith("paused"):
                reply = self.say(ws.start[0], "the session paused itself: reopen the work")
            elif stuck and "?" in reply:
                value = next(iter(self.ruling(st["next"]).values()))
                reply = self.say(f"It is {value}.", "answer the model's question from the ruling")
            else:
                reply = self.say(self.next_phrase(work), "ask for the next item")
        return self.summary()

    def next_phrase(self, work) -> str:
        """How the scripted operator asks for the next item: the goal's own
        request, which is what the system re-issues in chat after each
        decision, and the phrase a serving keg is triggered by. (The start
        phrase asks for the same work but does not trigger a keg, so using it
        would send every item to a model; --next-phrase start is for
        diagnosis only.)"""
        if self.args.next_phrase == "request":
            return work.config.keg.request
        return work.config.work_session.start[0]

    def save_checkpoint(self, name: str) -> None:
        from grove import checkpoints

        saved = checkpoints.save(name, f"harness-driven, seed {self.args.seed}",
                                 surface="harness", replace=True)
        self.note(action=f"checkpoint {name}", reply=json.dumps(saved.get("goals")))

    def summary(self) -> dict:
        from grove import audit

        g = audit.economics(goal=self.goal)["goals"][0]
        out = {"session": self.session, "seed": self.args.seed, "turns": self.turns,
               "transcript": str(self.log_path), "rulings_inferred": self.inferred,
               "periods": [{k: p.get(k) for k in (
                   "label", "units", "model_units", "keg_units", "model_calls", "cost",
                   "cost_per_unit", "cost_source", "fully_priced", "seconds_per_unit")}
                   for p in g.get("periods") or []]}
        self.note(action="summary", reply=json.dumps(out))
        return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--goal", default="gl-invoice-coding")
    parser.add_argument("--build-policy", action="store_true")
    parser.add_argument("--runs", default="", help="runs to read rulings from, e.g. 12-17 or 12,15,17")
    parser.add_argument("--out", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--reset", action="store_true", help="open a new, harness-labelled run first")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--months", type=int, default=3, help="stop after this many months")
    parser.add_argument("--checkpoint", default="", help="save this checkpoint when --months is reached")
    parser.add_argument("--checkpoint-at", default="",
                        help="save checkpoints on the way, e.g. 2:before-month-3,3:all-done")
    parser.add_argument("--no-earn-phrase", dest="earn_phrase", action="store_false")
    parser.add_argument("--next-phrase", choices=["start", "request"], default="request")
    parser.add_argument("--max-turns", type=int, default=400)
    parser.add_argument("--transcript", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.build_policy:
        runs = set()
        for part in args.runs.split(","):
            if "-" in part:
                a, b = part.split("-")
                runs |= set(range(int(a), int(b) + 1))
            elif part.strip():
                runs.add(int(part))
        if not runs or not args.out:
            parser.error("--build-policy needs --runs and --out")
        policy = build_policy(args.goal, runs)
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(policy, indent=1, sort_keys=True), encoding="utf-8")
        kinds = Counter(v["from"] for v in policy["items"].values())
        split = [i for i, v in policy["items"].items() if len(v["votes"]) > 1]
        print(f"policy for {len(policy['items'])} items from runs {sorted(runs)}: {dict(kinds)}; "
              f"{len(split)} with more than one past ruling: {sorted(split)}")
        return 0

    if not args.policy:
        parser.error("--policy is required to run")
    policy = json.loads(Path(args.policy).expanduser().read_text(encoding="utf-8"))
    if args.reset:
        from grove.api.actions import _end_goal_sessions
        from grove.decision_work import config_for_goal, reset_work

        label = (f"harness-driven · seed {args.seed} · scripted operator replaying the "
                 f"operator's past rulings; live models")
        done = reset_work(config_for_goal(args.goal), label, surface="harness")
        _end_goal_sessions(args.goal)
        print(f"opened run {done['run']} ({label})")
    harness = Harness(args, policy)
    print(f"session {harness.session}; transcript {harness.log_path}")
    result = harness.run()
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
