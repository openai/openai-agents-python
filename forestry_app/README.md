# Forestry MultiAgent System

A modern web application for AI-powered forestry operations management featuring 11 specialized agents (A-K) working together to optimize workflows.

## Features

- **11 Specialized AI Agents (A-K)**:
  - A) Run Manager - Scheduling, planning, deadlines
  - B) Data Readiness - Data quality, preflight checks
  - C) LUT/Threshold Strategy - Parameters, tradeoffs
  - D) Post-Processing - Raster to polygon, filtering
  - E) QA/QC - Quality validation
  - F) Debug Triage - Error troubleshooting
  - G) Operational Feasibility - Contractor lens
  - H) Feedback Synth - Feedback analysis
  - I) Adoption & Impact - Metrics, ROI
  - J) Communications - Messaging, emails
  - K) Librarian - Documentation, playbook

- **Modern UI**: React with Tailwind CSS, responsive design
- **Live Chat**: Real-time conversations with WebSocket support
- **Intelligent Routing**: Auto-routes messages to appropriate agents
- **Team Presets**: Quick selection of pre-configured agent teams
- **Persistent Storage**: PostgreSQL for chats, plans, and data
- **Plans Management**: Create and execute multi-agent plans

## Tech Stack

- **Frontend**: React 18, TypeScript, Tailwind CSS, Vite
- **Backend**: FastAPI, Python 3.11
- **Database**: PostgreSQL with SQLAlchemy (async)
- **AI**: OpenAI GPT-4o
- **Deployment**: Docker, Railway

## Quick Start

### Prerequisites

- Node.js 20+
- Python 3.11+
- PostgreSQL database
- OpenAI API key

### Local Development

1. **Clone and navigate**:
   ```bash
   cd forestry_app
   ```

2. **Set up environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Install backend dependencies**:
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

4. **Install frontend dependencies**:
   ```bash
   cd ../frontend
   npm install
   ```

5. **Run development servers**:

   Terminal 1 (Backend):
   ```bash
   cd backend
   python main.py
   ```

   Terminal 2 (Frontend):
   ```bash
   cd frontend
   npm run dev
   ```

6. **Access the app**: http://localhost:3000

### Default Credentials

- Username: `MelissaBoch`
- Password: `light1way`

## Railway Deployment

1. Create a new Railway project
2. Add PostgreSQL service
3. Add your service from this repository
4. Set environment variables:
   - `OPENAI_API_KEY`: Your OpenAI API key
   - `SECRET_KEY`: Random secure string for JWT
   - `DATABASE_URL`: (Auto-set by Railway PostgreSQL)

Railway will automatically:
- Build using the Dockerfile
- Run health checks
- Connect to PostgreSQL

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection string | Yes |
| `OPENAI_API_KEY` | OpenAI API key | Yes |
| `SECRET_KEY` | JWT signing secret | Yes |
| `PORT` | Server port (default: 8000) | No |
| `DEBUG` | Enable debug mode | No |

## API Endpoints

- `POST /api/auth/token` - Login
- `GET /api/auth/me` - Get current user
- `GET /api/agents/` - List all agents
- `GET /api/agents/teams/default` - Get preset teams
- `POST /api/agents/route` - Auto-route message
- `GET /api/chats/` - List chats
- `POST /api/chats/` - Create chat
- `POST /api/chats/{id}/messages` - Send message
- `WS /api/chats/ws/{id}` - WebSocket chat
- `GET /api/plans/` - List plans
- `POST /api/plans/{id}/execute` - Execute plan

## Architecture

```
forestry_app/
├── backend/
│   ├── agents/          # Agent definitions & manager
│   ├── models/          # Database models & schemas
│   ├── routes/          # API endpoints
│   ├── services/        # Auth & business logic
│   ├── config.py        # Configuration
│   └── main.py          # FastAPI app
├── frontend/
│   ├── src/
│   │   ├── components/  # React components
│   │   ├── hooks/       # Custom hooks
│   │   ├── lib/         # API client
│   │   ├── pages/       # Page components
│   │   └── styles/      # CSS/Tailwind
│   └── public/          # Static assets
├── Dockerfile           # Multi-stage Docker build
└── railway.toml         # Railway config
```

## Agent Routing

Default routing when request is unclear: **B + E + G**
- B) Data Readiness
- E) QA/QC
- G) Operational Feasibility

Messages are automatically routed to the most relevant agents based on content analysis.

## License

MIT License
