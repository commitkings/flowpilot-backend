# FlowPilot

> Intelligent payout operations layer on top of Interswitch APIs.

FlowPilot is an AI-driven payout automation platform built for the **Interswitch Buildathon 2026**. It sits **on top of** Interswitch and acts as an intelligent operations layer for batch payouts—handling run setup via conversational AI, risk-based candidate scoring, human approval gates, real bank account verification (BAV), and auditable execution trails.

**FlowPilot helps you:**
- Describe a payout in natural language
- Score payout candidates for risk (`allow`, `review`, `block`)
- Pause for human approval before execution
- Verify beneficiaries against Interswitch Bank Account Verification
- Execute approved payouts with full audit trail

## Features

- **Conversational Run Setup** — Chat assistant extracts objective, dates, risk tolerance, and candidates naturally
- **Multi-Agent Pipeline** — 5 backend agents (Planner → Reconciliation → Risk → Execution → Audit) orchestrated after run creation
- **Risk Scoring** — Each payout candidate scored and classified as `allow`, `review`, or `block`
- **Human-in-the-Loop** — Approval gate pauses execution until you approve flagged candidates
- **Real Bank Account Verification** — Calls Interswitch BAV API to resolve and match beneficiary names
- **Full Audit Trail** — Every run step persisted for compliance and post-run reporting

## Architecture

FlowPilot has **two layers of AI**:

1. **Chat Assistant (Intent Agent)** — Before the run exists; extracts slots, asks for missing info, prepares run config
2. **Orchestration Pipeline** — After run creation; 5 agents execute in sequence with an approval checkpoint

```
┌─────────────────────────────────────────────────────────────┐
│                    Chat Assistant (Intent Agent)             │
│         "What is this payout for?" → Slot extraction         │
└────────────────────────────┬────────────────────────────────┘
                             │ Create Run
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                   Orchestration Pipeline                     │
│  ┌──────────┐  ┌────────────┐  ┌──────┐  ┌─────────────────┐│
│  │ Planner  │→ │Reconcilia- │→ │ Risk │→ │ Approval Gate   ││
│  │          │  │   tion     │  │      │  │ (Human Review)  ││
│  └──────────┘  └────────────┘  └──────┘  └────────┬────────┘│
│                                                    │         │
│                           ┌────────────────────────┘         │
│                           ▼                                  │
│              ┌───────────────────────┐  ┌───────────┐       │
│              │      Execution        │→ │   Audit   │       │
│              │ (BAV + Payout/Sim)    │  │           │       │
│              └───────────────────────┘  └───────────┘       │
└─────────────────────────────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
       PostgreSQL        Redis          Groq LLM
```

**Core Agents:**

| Agent                   | Responsibility                                                           |
| ----------------------- | ------------------------------------------------------------------------ |
| **PlannerAgent**        | Decomposes objective into executable multi-step plan                     |
| **ReconciliationAgent** | Pulls transaction context for the payout window                          |
| **RiskAgent**           | Scores candidates, assigns `allow`/`review`/`block` decisions            |
| **ExecutionAgent**      | Calls Interswitch BAV, matches names, executes or simulates payouts      |
| **AuditAgent**          | Generates post-run compliance report with full trace                     |

> [!NOTE]
> The **Approval Gate** is not an AI agent—it's where **you** make the decision on flagged candidates.

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL 17+
- Redis 7+
- [Groq API key](https://console.groq.com/)
- [Interswitch API credentials](https://developer.interswitchgroup.com/) (for payment features)

### Local Development

1. **Clone the repository**

   ```bash
   git clone https://github.com/your-org/flowpilot.git
   cd flowpilot
   ```
2. **Set up environment**

   ```bash
   cp .env.example .env
   # Edit .env with your API keys and database credentials
   ```
3. **Install dependencies**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. **Run database migrations**

   ```bash
   alembic upgrade head
   ```
5. **Start the server**

   ```bash
   uvicorn app.api.app:app --reload --port 8000
   ```

### Docker Setup

```bash
docker compose up -d
```

This starts three services:

- **flowpilot** — FastAPI application on port `8000`
- **postgres** — PostgreSQL database on port `5432`
- **redis** — Redis cache on port `6379`

> [!NOTE]
> The Docker setup uses a multi-stage build for optimized image size.

## Project Structure

```
flowpilot/
├── app/                      # FastAPI application
│   └── api/                  # API routes and middleware
├── src/                      # Core source code
│   ├── agents/               # AI agent implementations
│   ├── application/          # Application interfaces
│   ├── config/               # Settings and configuration
│   ├── domain/               # Domain models and entities
│   ├── infrastructure/       # Database, external services
│   ├── services/             # Business logic services
│   └── utilities/            # Logging, helpers
├── tests/                    # Test suite
├── docs/                     # Documentation
├── architecture/             # Architecture docs
├── scripts/                  # Utility scripts
├── docker-compose.yml        # Docker orchestration
├── Dockerfile                # Container build
├── alembic.ini               # Database migrations config
└── requirements.txt          # Python dependencies
```

## API Reference

The API is available at `http://localhost:8000` with interactive docs at `/docs`.

| Endpoint               | Description                               |
| ---------------------- | ----------------------------------------- |
| `POST /agent/invoke` | Invoke an agent with a specific objective |
| `GET /sessions`      | List conversation sessions                |
| `GET /transactions`  | Query transaction history                 |
| `POST /upload`       | Upload files for processing               |
| `GET /interswitch/*` | Interswitch API proxy endpoints           |

## Configuration

Key environment variables (see `.env.example` for complete list):

| Variable                   | Description                   |
| -------------------------- | ----------------------------- |
| `DATABASE_URL`           | PostgreSQL connection string  |
| `REDIS_URL`              | Redis connection string       |
| `GROQ_API_KEY`           | Groq API key for LLM          |
| `INTERSWITCH_CLIENT_ID`  | Interswitch OAuth client ID   |
| `INTERSWITCH_SECRET_KEY` | Interswitch OAuth secret      |
| `GOOGLE_CLIENT_ID`       | Google OAuth client ID (auth) |

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_agents.py -v
```

## Resources

- [Architecture Documentation](./architecture/HACKATHON.md) — Detailed system design
- [Live Test Guide](./docs/LIVE_TEST_GUIDE.md) — Step-by-step usage guide for operators and reviewers
- [API Documentation](http://localhost:8000/docs) — Interactive Swagger UI
- [Interswitch Developer Portal](https://developer.interswitchgroup.com/) — Payment API docs
