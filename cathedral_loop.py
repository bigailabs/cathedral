"""Cathedral's signed model-improvement loop in 100 physical lines."""
# ruff: noqa: E701, E702
import json
from base64 import b64decode, b64encode
from hashlib import sha256
from pathlib import Path
from subprocess import TimeoutExpired, run
from cryptography.hazmat.primitives import serialization; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
class Reject(ValueError): pass
def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
def digest(value): return "sha256:" + sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()
def pinned(value): return isinstance(value, str) and len(value) == 71 and value.startswith("sha256:") and all(char in "0123456789abcdef" for char in value[7:])
def public(key): return b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
def signature(key, value): return b64encode(key.sign(canonical(value))).decode()
def require(condition, message):
    if not condition: raise Reject(message)
def vector(claims, expected, burn, epoch):
    require(type(epoch) is int and epoch >= 0 and burn not in expected and bool(expected) and all(type(value) is int and value > 0 for value in expected.values()), "invalid entitlements"); verified = {}
    for claim in claims:
        if claim["kind"] in {"work", "compute"} and claim["payload"]["epoch"] == epoch: verified[claim["actor"]] = verified.get(claim["actor"], 0) + claim["payload"]["units"]
    total = sum(expected.values())
    weights = {actor: 900_000 * min(verified.get(actor, 0), cap) // total for actor, cap in expected.items()}
    weights[burn] = 1_000_000 - sum(weights.values())
    return weights
class Cathedral:
    def __init__(self, authority, quote_verifier, verifier_digest, trainer, trainer_digest, evaluator, evaluator_digest, base_model=b"", burn="burn"):
        self.authority, self.quote_verifier, self.trainer, self.evaluator = authority, quote_verifier, trainer, evaluator
        self.verifier_digest, self.trainer_digest, self.evaluator_digest = verifier_digest, trainer_digest, evaluator_digest; require(all(pinned(value) for value in (verifier_digest, trainer_digest, evaluator_digest)), "unpinned runtime")
        self.authority_key, self.burn, self.log = public(authority), burn, []
        self.keys, self.teachers = {"cathedral": self.authority_key}, {}
        self.model, self.model_digest = base_model, digest(base_model)
    def _append(self, kind, actor, payload, signer):
        require(self.keys.get(actor) == public(signer), "unknown or mismatched actor")
        claim = {"kind": kind, "actor": actor, "payload": payload}
        body = {"previous": self.log[-1]["id"] if self.log else "genesis", "claim": claim, "actor_signature": signature(signer, claim)}
        item = {**body, "id": digest(body)}
        item["validator_signature"] = signature(self.authority, item)
        self.log.append(item); return item
    def register(self, actor, key):
        require(bool(actor) and actor not in self.keys and actor != self.burn, "invalid actor")
        item = self._append("registry", "cathedral", {"actor": actor, "key": public(key)}, self.authority)
        self.keys[actor] = public(key); return item
    def license_teacher(self, teacher, model, revision, license_digest, active=True):
        require(all((teacher, model, revision)) and pinned(license_digest), "unpinned teacher licence")
        item = self._append("licence", "cathedral", {"teacher": teacher, "model": model, "revision": revision, "license_digest": license_digest, "active": bool(active)}, self.authority)
        self.teachers[teacher] = item; return item
    def work(self, actor, signer, epoch, task_digest, witness, vulnerable, patched, trace=b"", timeout=10):
        require(type(epoch) is int and epoch >= 0 and pinned(task_digest), "unpinned task")
        witness = Path(witness); witness_digest = digest(witness.read_bytes())
        def execute(binary):
            binary = Path(binary); build = digest(binary.read_bytes()); result = run([str(binary), str(witness)], capture_output=True, timeout=timeout)
            return {"build": build, "exit": result.returncode, "output": digest(result.stdout + result.stderr)}
        try: vulnerable_result, patched_result = execute(vulnerable), execute(patched)
        except (OSError, TimeoutExpired) as exc: raise Reject("replay failed") from exc
        require(vulnerable_result["exit"] < 0 and patched_result["exit"] == 0, "witness did not isolate the vulnerable build")
        payload = {"epoch": epoch, "task": task_digest, "model": self.model_digest, "trace": digest(trace), "witness": witness_digest, "vulnerable": vulnerable_result, "patched": patched_result, "units": 1}
        return self._append("work", actor, payload, signer)
    def _attest(self, actor, signer, epoch, quote, workload):
        claims = self.quote_verifier(quote, workload)
        require(claims.get("valid") is True and claims.get("report_data") == workload, "attestation did not bind the workload")
        return self._append("compute", actor, {"epoch": epoch, "workload": workload, "quote": digest(quote), "measurement": claims.get("measurement", ""), "verifier": self.verifier_digest, "units": 1}, signer)
    def distill(self, epoch, teacher, compute_actor, compute_signer, quote, recipe):
        licence = self.teachers.get(teacher)
        require(type(epoch) is int and epoch >= 0 and licence is not None and licence["claim"]["payload"]["active"] is True, "teacher is not licensed")
        members = [item["id"] for item in self.log if item["claim"]["kind"] == "work"]
        require(bool(members), "verified corpus is empty")
        corpus = self._append("corpus", "cathedral", {"members": members, "digest": digest(members)}, self.authority)
        workload = digest({"corpus": corpus["id"], "teacher": licence["id"], "recipe": digest(recipe), "trainer": self.trainer_digest})
        compute = self._attest(compute_actor, compute_signer, epoch, quote, workload)
        artifact = self.trainer(tuple(members), licence["claim"]["payload"], recipe)
        require(isinstance(artifact, bytes) and bool(artifact), "trainer produced no checkpoint")
        training = self._append("training", "cathedral", {"corpus": corpus["id"], "teacher": licence["id"], "compute": compute["id"], "recipe": digest(recipe), "trainer": self.trainer_digest, "artifact": digest(artifact)}, self.authority)
        before, after = list(self.evaluator(self.model)), list(self.evaluator(artifact))
        require(bool(before) and len(before) == len(after) and all(type(x) is bool for x in before + after), "sealed evaluation is invalid")
        improved = sum(after) > sum(before)
        evaluation = self._append("evaluation", "cathedral", {"training": training["id"], "evaluator": self.evaluator_digest, "results": digest({"before": before, "after": after}), "before": sum(before), "after": sum(after), "improved": improved}, self.authority)
        require(improved, "candidate did not improve")
        checkpoint = self._append("checkpoint", "cathedral", {"training": training["id"], "evaluation": evaluation["id"], "previous": self.model_digest, "artifact": digest(artifact)}, self.authority)
        self.model, self.model_digest = artifact, digest(artifact); return checkpoint
    def weights(self, epoch, entitlements):
        return self._append("weights", "cathedral", {"epoch": epoch, "entitlements": entitlements, "vector": vector((item["claim"] for item in self.log), entitlements, self.burn, epoch)}, self.authority)
    def verify(self):
        keys, previous, seen = {"cathedral": self.authority_key}, "genesis", {}
        refs = {"corpus": {"members": "work"}, "training": {"corpus": "corpus", "teacher": "licence", "compute": "compute"}, "evaluation": {"training": "training"}, "checkpoint": {"training": "training", "evaluation": "evaluation"}}
        for item in self.log:
            claim = item["claim"]
            body = {"previous": item["previous"], "claim": claim, "actor_signature": item["actor_signature"]}
            require(item["previous"] == previous and item["id"] == digest(body), "broken receipt chain")
            Ed25519PublicKey.from_public_bytes(b64decode(keys[claim["actor"]])).verify(b64decode(item["actor_signature"]), canonical(claim))
            Ed25519PublicKey.from_public_bytes(b64decode(self.authority_key)).verify(b64decode(item["validator_signature"]), canonical({**body, "id": item["id"]}))
            payload = claim["payload"]
            for field, expected in refs.get(claim["kind"], {}).items():
                values = payload[field] if isinstance(payload[field], list) else [payload[field]]
                require(all(value in seen and seen[value]["kind"] == expected for value in values), "missing evidence")
            if claim["kind"] == "weights": require(payload["vector"] == vector(seen.values(), payload["entitlements"], self.burn, payload["epoch"]), "invalid weight vector")
            seen[item["id"]], previous = claim, item["id"]
            if claim["kind"] == "registry":
                require(payload["actor"] not in keys, "duplicate actor registry")
                keys[payload["actor"]] = payload["key"]
        return True
