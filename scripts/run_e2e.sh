#!/usr/bin/env bash
# ==============================================================================
# AI Development Pipeline - LLM End-to-End (E2E) Automated Verification Script
# ==============================================================================
set -euo pipefail

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# 1. Environment & Agent setup
PYTHON_CMD="python3"
if [ -f "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON_CMD="$ROOT_DIR/.venv/bin/python"
fi

AGENT="${E2E_AGENT:-antigravity}"
WORK_DIR="${E2E_WORK:-/tmp/dev-pipeline-llm-e2e}"
PROJ_ID="calc-app"

echo -e "${BLUE}================================================================${NC}"
echo -e "${BLUE}   AI Development Pipeline - LLM E2E Verification Running       ${NC}"
echo -e "${BLUE}================================================================${NC}"
echo -e "Target Agent:      ${YELLOW}$AGENT${NC}"
echo -e "Work Directory:    ${YELLOW}$WORK_DIR${NC}"
echo -e "Python Executable: ${YELLOW}$PYTHON_CMD${NC}"
echo ""

# Cleanup previous test run
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# ------------------------------------------------------------------------------
echo -e "${BLUE}[Step 1/6] Initializing Project '$PROJ_ID'...${NC}"
# ------------------------------------------------------------------------------
$PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" \
  project init "$PROJ_ID" \
  --agent "$AGENT" \
  --check "test=$PYTHON_CMD -m unittest discover -v"

if [ ! -f "$WORK_DIR/$PROJ_ID/project.json" ]; then
    echo -e "${RED}[FAIL] Project initialization failed: project.json not found.${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Project initialized successfully.${NC}\n"

# ------------------------------------------------------------------------------
echo -e "${BLUE}[Step 2/6] Creating & Running Issue 1 (0001-basic-math)...${NC}"
echo -e "${YELLOW}※ LLM is planning, implementing, and reviewing. This usually takes 1-3 minutes. Please wait...${NC}"
# ------------------------------------------------------------------------------
ISSUE_1_OUT=$($PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" \
  issue create "$PROJ_ID" \
  --id 0001-basic-math \
  --request "簡単な計算モジュール calc.py と単体テスト test_calc.py を作成してください。加算 add(a, b) と減算 subtract(a, b) を提供し、python3 -m unittest で全テストが通るようにしてください。" \
  --requirement "calc.py に add(a, b) と subtract(a, b) 関数があること" \
  --requirement "test_calc.py に unittest を用いたテストケースがあること")

echo "$ISSUE_1_OUT"

STATUS_1=$($PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" issue status "$PROJ_ID" 0001-basic-math | grep '"state"' | tr -d '", ' | cut -d: -f2)
if [ "$STATUS_1" != "AWAITING_HUMAN_CHECK" ]; then
    echo -e "${RED}[FAIL] Issue 1 did not reach AWAITING_HUMAN_CHECK. Current state: $STATUS_1${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Issue 1 reached AWAITING_HUMAN_CHECK (READY).${NC}\n"

# ------------------------------------------------------------------------------
echo -e "${BLUE}[Step 3/6] Approving Issue 1 (ACCEPT)...${NC}"
# ------------------------------------------------------------------------------
$PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" \
  issue human-check "$PROJ_ID" 0001-basic-math ACCEPT

STATUS_1_AFTER=$($PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" issue status "$PROJ_ID" 0001-basic-math | grep '"state"' | tr -d '", ' | cut -d: -f2)
if [ "$STATUS_1_AFTER" != "RESOLVED" ]; then
    echo -e "${RED}[FAIL] Issue 1 state is not RESOLVED. Current state: $STATUS_1_AFTER${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Issue 1 successfully RESOLVED.${NC}\n"

# ------------------------------------------------------------------------------
echo -e "${BLUE}[Step 4/6] Creating & Running Issue 2 (0002-multiply)...${NC}"
echo -e "${YELLOW}※ LLM is incorporating resolved context from Issue 1 and adding multiply. Please wait...${NC}"
# ------------------------------------------------------------------------------
ISSUE_2_OUT=$($PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" \
  issue create "$PROJ_ID" \
  --id 0002-multiply \
  --request "既存の calc.py に乗算 multiply(a, b) 関数を追加してください。既存のテストを壊さずに test_calc.py に乗算のテストも追加してください。" \
  --requirement "既存の add, subtract 関数がそのまま動作すること" \
  --requirement "multiply(a, b) が正常に計算できること")

echo "$ISSUE_2_OUT"

STATUS_2=$($PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" issue status "$PROJ_ID" 0002-multiply | grep '"state"' | tr -d '", ' | cut -d: -f2)
if [ "$STATUS_2" != "AWAITING_HUMAN_CHECK" ]; then
    echo -e "${RED}[FAIL] Issue 2 did not reach AWAITING_HUMAN_CHECK. Current state: $STATUS_2${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Issue 2 reached AWAITING_HUMAN_CHECK (READY).${NC}\n"

# ------------------------------------------------------------------------------
echo -e "${BLUE}[Step 5/6] Approving Issue 2 (ACCEPT)...${NC}"
# ------------------------------------------------------------------------------
$PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" \
  issue human-check "$PROJ_ID" 0002-multiply ACCEPT

STATUS_2_AFTER=$($PYTHON_CMD -m dev_pipeline.cli --work-dir "$WORK_DIR" issue status "$PROJ_ID" 0002-multiply | grep '"state"' | tr -d '", ' | cut -d: -f2)
if [ "$STATUS_2_AFTER" != "RESOLVED" ]; then
    echo -e "${RED}[FAIL] Issue 2 state is not RESOLVED. Current state: $STATUS_2_AFTER${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Issue 2 successfully RESOLVED.${NC}\n"

# ------------------------------------------------------------------------------
echo -e "${BLUE}[Step 6/6] Verifying Final Artifacts, Git History, and Tests...${NC}"
# ------------------------------------------------------------------------------
REPO_DIR="$WORK_DIR/$PROJ_ID/repo"

echo "Checking generated calc.py content:"
cat "$REPO_DIR/calc.py"
echo ""

# Check all functions exist
for func in "add" "subtract" "multiply"; do
    if ! grep -q "def $func" "$REPO_DIR/calc.py"; then
        echo -e "${RED}[FAIL] Function '$func' not found in calc.py!${NC}"
        exit 1
    fi
done
echo -e "${GREEN}[OK] All functions (add, subtract, multiply) exist in calc.py.${NC}"

# Run unittests
echo "Running full unit test suite:"
(cd "$REPO_DIR" && $PYTHON_CMD -m unittest discover -v)
echo -e "${GREEN}[OK] Unit tests passed!${NC}"

# Check Git commit count / status
if git -C "$REPO_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
    COMMIT_COUNT=$(git -C "$REPO_DIR" rev-list --count HEAD)
    echo "Git Commit Count: $COMMIT_COUNT"
    git -C "$REPO_DIR" log --oneline
else
    echo "Git Commit Count: 0 (working tree contains generated files)"
    git -C "$REPO_DIR" status --short
fi

echo ""
echo -e "${BLUE}================================================================${NC}"
echo -e "${GREEN}   E2E VERIFICATION COMPLETED SUCCESSFULLY! (ALL TESTS PASSED)  ${NC}"
echo -e "${BLUE}================================================================${NC}"
echo -e "Project: $PROJ_ID"
echo -e "Issues Verified: 0001-basic-math [RESOLVED], 0002-multiply [RESOLVED]"
echo -e "Artifacts Location: $REPO_DIR"
echo -e "Clean up when done: rm -rf $WORK_DIR"
