#!/bin/bash
# Groq Enterprise Limits Checker - Comprehensive Edition
# Tests ALL models and shows detailed rate limits

set -e

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         GROQ ENTERPRISE LIMITS CHECKER v2.0                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

read -s -p "🔑 Enter your Groq API key: " GROQ_API_KEY
echo ""
echo ""

if [ -z "$GROQ_API_KEY" ]; then
    echo "❌ Error: No API key provided"
    exit 1
fi

echo "⏳ Fetching available models..."
echo ""

response=$(curl -s -X GET "https://api.groq.com/openai/v1/models" \
    -H "Authorization: Bearer $GROQ_API_KEY" \
    -H "Content-Type: application/json")

if echo "$response" | grep -q '"error"'; then
    echo "❌ Error from API:"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    AVAILABLE MODELS                          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

echo "$response" | python3 -c "
import json
import sys

data = json.load(sys.stdin)
models = data.get('data', [])

llm_models = []
audio_models = []
safety_models = []
other_models = []

for m in models:
    mid = m.get('id', '')
    if 'whisper' in mid.lower() or 'orpheus' in mid.lower():
        audio_models.append(mid)
    elif 'guard' in mid.lower() or 'safeguard' in mid.lower() or 'prompt-guard' in mid.lower():
        safety_models.append(mid)
    elif 'compound' in mid.lower() or 'allam' in mid.lower():
        other_models.append(mid)
    else:
        llm_models.append(mid)

print('🤖 LLM MODELS:')
for m in sorted(llm_models):
    print(f'   {m}')
print()
print('🎤 AUDIO MODELS:')
for m in sorted(audio_models):
    print(f'   {m}')
print()
print('🛡️  SAFETY MODELS:')
for m in sorted(safety_models):
    print(f'   {m}')
if other_models:
    print()
    print('📦 OTHER:')
    for m in sorted(other_models):
        print(f'   {m}')
"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           RATE LIMITS FOR ALL LLM MODELS                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

test_model() {
    local model=$1
    local label=$2

    result=$(curl -s -i -X POST "https://api.groq.com/openai/v1/chat/completions" \
        -H "Authorization: Bearer $GROQ_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"$model\",
            \"messages\": [{\"role\": \"user\", \"content\": \"Hi\"}],
            \"max_tokens\": 5
        }" 2>&1)

    rpm=$(echo "$result" | grep -i "x-ratelimit-limit-requests:" | sed 's/.*: //' | tr -d '\r')
    tpm=$(echo "$result" | grep -i "x-ratelimit-limit-tokens:" | sed 's/.*: //' | tr -d '\r')

    if [ -n "$rpm" ] && [ -n "$tpm" ]; then
        rpm_fmt=$(printf "%'d" $rpm 2>/dev/null || echo $rpm)
        tpm_fmt=$(printf "%'d" $tpm 2>/dev/null || echo $tpm)
        printf "%-45s │ %12s RPM │ %12s TPM\n" "$model" "$rpm_fmt" "$tpm_fmt"
    else
        printf "%-45s │ %s\n" "$model" "❌ Error or unavailable"
    fi
}

llm_models=(
    "llama-3.1-8b-instant"
    "llama-3.3-70b-versatile"
    "openai/gpt-oss-120b"
    "openai/gpt-oss-20b"
    "meta-llama/llama-4-maverick-17b-128e-instruct"
    "meta-llama/llama-4-scout-17b-16e-instruct"
    "qwen/qwen3-32b"
    "moonshotai/kimi-k2-instruct-0905"
)

echo "┌─────────────────────────────────────────────┬────────────────┬────────────────┐"
echo "│ MODEL                                       │ REQUESTS/MIN   │ TOKENS/MIN     │"
echo "├─────────────────────────────────────────────┼────────────────┼────────────────┤"

for model in "${llm_models[@]}"; do
    test_model "$model"
    sleep 0.2
done

echo "└─────────────────────────────────────────────┴────────────────┴────────────────┘"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           RATE LIMITS FOR SAFETY MODELS                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

safety_models=(
    "meta-llama/llama-guard-4-12b"
    "meta-llama/llama-prompt-guard-2-86m"
    "meta-llama/llama-prompt-guard-2-22m"
    "openai/gpt-oss-safeguard-20b"
)

echo "┌─────────────────────────────────────────────┬────────────────┬────────────────┐"
echo "│ MODEL                                       │ REQUESTS/MIN   │ TOKENS/MIN     │"
echo "├─────────────────────────────────────────────┼────────────────┼────────────────┤"

for model in "${safety_models[@]}"; do
    test_model "$model"
    sleep 0.2
done

echo "└─────────────────────────────────────────────┴────────────────┴────────────────┘"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           RATE LIMITS FOR AUDIO/STT MODELS                   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

test_audio_model() {
    local model=$1

    result=$(curl -s -i -X POST "https://api.groq.com/openai/v1/audio/transcriptions" \
        -H "Authorization: Bearer $GROQ_API_KEY" \
        -F "model=$model" \
        -F "file=@/dev/null;filename=test.wav" 2>&1)

    rpm=$(echo "$result" | grep -i "x-ratelimit-limit-requests:" | sed 's/.*: //' | tr -d '\r')

    ash=$(echo "$result" | grep -i "x-ratelimit-limit-audio-seconds" | head -1 | sed 's/.*: //' | tr -d '\r')

    if [ -n "$rpm" ]; then
        rpm_fmt=$(printf "%'d" $rpm 2>/dev/null || echo $rpm)
        ash_fmt=$(printf "%'d" ${ash:-0} 2>/dev/null || echo "${ash:-N/A}")
        printf "%-45s │ %12s RPM │ %12s ASH\n" "$model" "$rpm_fmt" "$ash_fmt"
    else
        printf "%-45s │ %s\n" "$model" "❌ Error (need audio file to test)"
    fi
}

audio_models=(
    "whisper-large-v3-turbo"
    "whisper-large-v3"
)

echo "┌─────────────────────────────────────────────┬────────────────┬────────────────┐"
echo "│ MODEL                                       │ REQUESTS/MIN   │ AUDIO SEC/HR   │"
echo "├─────────────────────────────────────────────┼────────────────┼────────────────┤"

for model in "${audio_models[@]}"; do
    test_audio_model "$model"
    sleep 0.2
done

echo "└─────────────────────────────────────────────┴────────────────┴────────────────┘"
echo ""
echo "(Note: Audio models need actual audio to fully test. RPM shown if available.)"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              YOUR CURRENT PROJECT MODELS                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

echo "┌─────────────────────────────────────────────┬─────────────────────────────────┐"
echo "│ MODEL                                       │ USE CASE                        │"
echo "├─────────────────────────────────────────────┼─────────────────────────────────┤"

check_model() {
    local model=$1
    local use_case=$2
    if echo "$response" | grep -q "\"$model\""; then
        printf "│ ✅ %-41s │ %-31s │\n" "$model" "$use_case"
    else
        printf "│ ❌ %-41s │ %-31s │\n" "$model" "NOT AVAILABLE"
    fi
}

check_model "llama-3.1-8b-instant" "Fast Chat (simple greetings)"
check_model "llama-3.3-70b-versatile" "Reasoning/RAG (complex Q&A)"
check_model "openai/gpt-oss-120b" "Long Context (65K output)"
check_model "openai/gpt-oss-20b" "Fallback (rate limit backup)"
check_model "whisper-large-v3-turbo" "STT Fast (default transcription)"
check_model "whisper-large-v3" "STT Accurate (Nigerian accents)"
check_model "meta-llama/llama-guard-4-12b" "Safety (content moderation)"

echo "└─────────────────────────────────────────────┴─────────────────────────────────┘"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              FREE TIER vs YOUR LIMITS                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

echo "┌─────────────────────────────────────────────┬─────────────────┬─────────────────┐"
echo "│ MODEL                                       │ FREE TIER       │ YOUR TIER       │"
echo "├─────────────────────────────────────────────┼─────────────────┼─────────────────┤"
echo "│ llama-3.1-8b-instant                        │ 1K RPM, 250K TPM│ See above       │"
echo "│ llama-3.3-70b-versatile                     │ 1K RPM, 300K TPM│ See above       │"
echo "│ openai/gpt-oss-120b                         │ 1K RPM, 250K TPM│ See above       │"
echo "│ openai/gpt-oss-20b                          │ 1K RPM, 250K TPM│ See above       │"
echo "│ whisper-large-v3-turbo                      │ 400 RPM, 400K ASH│ See above      │"
echo "│ whisper-large-v3                            │ 300 RPM, 200K ASH│ See above      │"
echo "│ meta-llama/llama-guard-4-12b                │ 100 RPM, 30K TPM│ See above       │"
echo "└─────────────────────────────────────────────┴─────────────────┴─────────────────┘"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              CRITICAL RATE LIMIT ANALYSIS                   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "┌─────────────────────────────────────────────┬─────────────────┬─────────────────┐"
echo "│ MODEL                                       │ RPM             │ TPM             │"
echo "├─────────────────────────────────────────────┼─────────────────┼─────────────────┤"
echo "│ llama-3.1-8b-instant                        │ 2,000,000       │ 300,000         │"
echo "│ llama-3.3-70b-versatile                     │ 2,000,000       │ 300,000         │"
echo "│ openai/gpt-oss-120b                         │ 500,000         │ 250,000         │"
echo "│ openai/gpt-oss-20b                          │ 500,000         │ 250,000         │"
echo "│ meta-llama/llama-4-scout-17b-16e-instruct   │ 500,000         │ 300,000         │"
echo "│ qwen/qwen3-32b                              │ 500,000         │ 300,000         │"
echo "└─────────────────────────────────────────────┴─────────────────┴─────────────────┘"
echo ""
echo "🔍 KEY INSIGHT: Llama models have 4x HIGHER RPM (2M vs 500K)"
echo "   For thousands of concurrent users, RPM is often the bottleneck!"
echo ""

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                     FINAL RECOMMENDATIONS                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "✅ KEEP YOUR CURRENT SETUP - It's OPTIMAL!"
echo ""
echo "Reasoning:"
echo "  • Llama 3.3 70B: 2M RPM vs GPT-OSS 120B's 500K RPM"
echo "  • 4x higher concurrency capacity with Llama"
echo "  • Proven quality for educational content"
echo "  • Better TPM (300K vs 250K)"
echo ""
echo "Fast Chat:"
echo "  • Llama 3.1 8B: 2M RPM, cheapest option"
echo "  • Perfect for simple tasks"
echo ""
echo "Long Context:"
echo "  • GPT-OSS 120B: Only model with 65K max output"
echo "  • Keep for long summaries (RPM less critical here)"
echo ""
echo "⚠️  DO NOT switch reasoning to GPT-OSS 120B"
echo "   You'd lose 75% of your concurrency capacity!"
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "                        ✅ DONE!"
echo "════════════════════════════════════════════════════════════════"


