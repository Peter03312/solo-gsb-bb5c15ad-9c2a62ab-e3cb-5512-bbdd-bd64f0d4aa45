#!/usr/bin/env bash
# 本轮用法端到端示例: 会务方删除已发布反馈的论文 (删除反馈生命周期)
#   录入 -> 预演/发布 -> 评审人确认/交评语 -> 发布反馈快照并收到作者异议 ->
#   会务方 DELETE 删除论文 (删除在同一事务原子生效):
#     该稿全部快照访问码立即失效 (持码读取/提交新异议与无效码同形统一 404),
#     待处理异议原子变为 expired (作者原查询凭据仍可查状态),
#     写入删除记录 (可核验的失效依据: 删除时刻/发布序号/槽位/失效与过期计数),
#     快照、异议、评语、更正等历史保留供会务方追溯; 删除推进资料修订号 +1 ->
#   同编号重新录入不恢复旧码: 旧码仍 404; 在旧序号重发快照 409 (不重新发放旧码);
#   重新发布 -> 重新确认/交评语 -> 重新发布快照才得到全新访问码 ->
#   迁移: 导出 (format_version 2, 含 paper_deletions) -> 空实例恢复,
#     旧码保持 404、新码可用; 删除失效依据随快照迁移并在恢复时跨记录核验
# 建议对新库运行: ./examples/paper-deletions.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 2 名不同机构评审人 + 1 篇论文 =="
for i in 1 2; do
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"R$i\", \"credential\": \"r$i-secret\", \"topics\": [\"AI\"],
    \"institution\": \"Inst-$i\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'

echo "== 2. 普通预演 + 发布 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SERIAL=$(echo "$PUB" | j 'd["serial"]')
cred() { echo "r$(echo "$1" | sed 's/R//')-secret"; }
A=$(echo "$PUB" | j 'd["plan"]["P1"][0]'); B=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
echo "P1 序号 $SERIAL 槽位: $A, $B"

echo "== 3. 两名评审人确认并各交一份正式评语 =="
n=1
for rid in "$A" "$B"; do
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $rid" -H "X-Reviewer-Credential: $(cred "$rid")" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $rid" -H "X-Reviewer-Credential: $(cred "$rid")" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": $((3+n)), \"comment\": \"$rid 对 P1 的评语\"}"
  n=$((n+1))
done

echo "== 4. 会务方发布反馈快照; 作者提交一条待处理异议 =="
CODE=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL}" | j 'd["access_code"]')
echo "快照访问码: $CODE"
TOKEN=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' -d "{
    \"access_code\": \"$CODE\", \"label\": 1, \"reason\": \"评语一存在事实错误, 请核查\"}" \
  | j 'd["query_token"]')
echo "作者异议查询凭据: $TOKEN"

echo "== 5. 会务方删除 P1: 反馈生命周期在同一事务原子收尾 =="
DEL=$(curl -s -X DELETE "$BASE/papers/P1" -H "$ORG")
echo "$DEL" | j "{'state': d['state'], 'revision': d['revision'],
        'serial_at_deletion': d['deletion']['published_serial'],
        'slots_at_deletion': d['deletion']['published_plan'],
        'invalidated_snapshots': d['invalidated_snapshots'],
        'expired_objections': d['expired_objections']}"
echo "-- 未知论文 404 / 已撤回论文 409 (沿用既有删除约定) --"
curl -s -o /dev/null -w '删除未知论文     -> HTTP %{http_code}\n' -X DELETE "$BASE/papers/NOPE" -H "$ORG"

echo "== 6. 删除后: 持码读取与提交新异议统一 404 (与无效码同形) =="
curl -s -o /dev/null -w '持码读取旧码     -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
curl -s -o /dev/null -w '无效码读取       -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/fbk-0000000000000000"
curl -s -o /dev/null -w '旧码提交新异议   -> HTTP %{http_code}\n' -X POST "$BASE/feedback-objections" \
  -H 'Content-Type: application/json' \
  -d "{\"access_code\": \"$CODE\", \"label\": 2, \"reason\": \"删除后试图提交\"}"

echo "== 7. 作者查询凭据仍可查 (状态 expired); 会务方可追溯全部历史 =="
curl -s "$BASE/feedback-objections/$TOKEN" | j "{'state': d['state'], 'resolved_at': d['resolved_at']}"
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j "{'versions': [s['version'] for s in d['snapshots']], 'active': [s['active'] for s in d['snapshots']]}"
curl -s "$BASE/papers/P1/objections" -H "$ORG" \
  | j "{'states': [o['state'] for o in d['objections']]}"
curl -s "$BASE/papers/P1/deletions" -H "$ORG" \
  | j "{'deletions': [{'revision': x['revision'], 'deleted_at': x['deleted_at'],
                        'invalidated': x['invalidated_snapshots'], 'expired': x['expired_objections']}
                       for x in d['deletions']]}"

echo "== 8. 同编号重新录入: 旧码不复活; 旧序号重发快照 409 =="
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 重新录入稿...", "topics": ["AI"], "institutions": ["Univ-X"]}'
curl -s -o /dev/null -w '旧码仍           -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
curl -s -o /dev/null -w '旧序号重发快照   -> HTTP %{http_code}\n' \
  -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"serial\": $SERIAL}"

echo "== 9. 重新发布并重新走确认/评语后, 发布得到全新访问码 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB2=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SERIAL2=$(echo "$PUB2" | j 'd["serial"]')
A2=$(echo "$PUB2" | j 'd["plan"]["P1"][0]'); B2=$(echo "$PUB2" | j 'd["plan"]["P1"][1]')
n=1
for rid in "$A2" "$B2"; do
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $rid" -H "X-Reviewer-Credential: $(cred "$rid")" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
    -H "X-Reviewer-Id: $rid" -H "X-Reviewer-Credential: $(cred "$rid")" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL2, \"score\": $((2+n)), \"comment\": \"$rid 重录后评语\"}"
  n=$((n+1))
done
NEW=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SERIAL2}")
NEWCODE=$(echo "$NEW" | j 'd["access_code"]')
echo "$NEW" | j "{'version': d['version'], 'new_code': d['access_code'], 'changed': d['changed']}"
curl -s -o /dev/null -w '新码可读         -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$NEWCODE"
curl -s -o /dev/null -w '旧码仍失效       -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
echo "新码 != 旧码: $([ "$NEWCODE" != "$CODE" ] && echo yes || echo NO)"

echo "== 10. 迁移导出 (format_version 2, 含删除失效依据) =="
curl -s "$BASE/migration/export" -H "$ORG" \
  | j "{'format': d['format'], 'format_version': d['format_version'],
        'deletions': d['data']['paper_deletions'],
        'old_code_invalidated': [s['invalidated'] for s in d['data']['feedback_snapshots']]}"
echo "把导出 JSON POST 到空实例的 /migration/restore 后: 旧码仍 404、新码可用,"
echo "且把旧快照 invalidated 改回有效 (重算 checksum) 的伪造快照会被 422 拒绝且不写部分数据。"
