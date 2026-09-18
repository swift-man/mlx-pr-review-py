#!/usr/bin/env python3
"""Qwen3-Coder-Next 교체 후 실전 검증.

실제 git diff 로 리뷰 payload 를 만들어 8002 /v1/generate 에 보내고,
(1) prefill+generate 소요시간 (2) strict JSON 파싱 성공 여부 를 확인한다.
"""
import json, os, pathlib, subprocess, sys, time, urllib.request

# 저장소 루트를 스크립트 위치에서 구한다. 절대 경로를 박으면 운영 러너
# (/Users/runner/pr-review) 나 다른 개발자 머신에서 바로 깨진다.
REPO = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
from review_runner.mlx_review_prompt import build_messages
from review_runner.mlx_review_parser import parse_and_normalize_model_output

URL = os.environ.get("MLX_GENERATE_URL", "http://127.0.0.1:8002/v1/generate")
BASE = os.environ.get("SMOKE_BASE", "HEAD~3")


def changed_files():
    names = subprocess.run(["git", "-C", REPO, "diff", "--name-only", BASE, "HEAD"],
                           capture_output=True, text=True).stdout.split()
    out = []
    for name in names:
        patch = subprocess.run(["git", "-C", REPO, "diff", "-U3", BASE, "HEAD", "--", name],
                               capture_output=True, text=True).stdout
        content = subprocess.run(["git", "-C", REPO, "show", f"HEAD:{name}"],
                                 capture_output=True, text=True).stdout
        if not patch:
            continue
        numbered = "\n".join(f"{i:>5} | {l}" for i, l in enumerate(content.splitlines(), 1))
        out.append({
            "path": name, "status": "modified",
            "additions": patch.count("\n+"), "deletions": patch.count("\n-"),
            "valid_comment_lines": list(range(1, min(len(content.splitlines()), 400) + 1)),
            "patch": patch,
            "current_file_context": numbered[:220000],
            "current_file_context_mode": "full_file",
        })
    return out


files = changed_files()
if not files:
    sys.exit(f"no diff between {BASE} and HEAD")

payload = {
    "pr_title": "smoke test: model swap verification",
    "pr_body": "Qwen3-Coder-Next 교체 검증용 실전 payload.",
    "instructions": {"language": "ko"},
    "files": files,
}
messages = build_messages(payload, max_findings=10)
prompt_chars = sum(len(m["content"]) for m in messages)

body = json.dumps({
    "messages": messages,
    "max_tokens": int(os.environ.get("MLX_MAX_TOKENS", "2000")),
    "temperature": 0.0, "top_p": 1.0,
}, ensure_ascii=False).encode()

print(f"files={len(files)}  prompt_chars={prompt_chars:,}  (~{prompt_chars//3.2:,.0f} tokens est)")
print(f"POST {URL}  body={len(body):,} bytes")

headers = {"Content-Type": "application/json"}
# generate 서버가 Bearer 인증을 쓰는 환경에서는 토큰이 없으면 401 로 검증이 실패한다.
auth_token = os.environ.get("MLX_GENERATE_AUTH_TOKEN", "").strip()
if auth_token:
    headers["Authorization"] = f"Bearer {auth_token}"
req = urllib.request.Request(URL, data=body, headers=headers)
t0 = time.monotonic()
with urllib.request.urlopen(req, timeout=900) as resp:
    data = json.loads(resp.read().decode())
wall = time.monotonic() - t0

text = data.get("text", "")
print(f"\n--- 응답 ---")
print(f"model        : {data.get('model')}")
print(f"wall         : {wall:.1f}s   (server elapsed_ms={data.get('elapsed_ms')})")
print(f"output chars : {len(text):,}")
print(f"throughput   : {prompt_chars/wall:,.0f} prompt-chars/s")

normalized, meta = parse_and_normalize_model_output(text, max_findings=10)
print(f"\n--- 파싱 ---")
print(f"parse_mode   : {meta.get('parse_mode')}")
if meta.get("parse_error"):
    print(f"parse_error  : {meta['parse_error'][:200]}")
print(f"event        : {normalized.get('event')}")
print(f"comments     : {len(normalized.get('comments', []))}")
print(f"summary      : {str(normalized.get('summary'))[:160]}")
for c in normalized.get("comments", [])[:3]:
    print(f"  - [{c.get('severity')}] {c.get('path')}:{c.get('line')} conf={c.get('confidence')}")
    print(f"    {str(c.get('body'))[:180]}")

ok = meta.get("parse_mode") != "fallback_response"
print(f"\nRESULT: {'PASS' if ok else 'FAIL — strict JSON 파싱 실패'}")
sys.exit(0 if ok else 1)
