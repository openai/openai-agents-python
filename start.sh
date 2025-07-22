#!/bin/bash
# Start frontend and backend for the Agentic Private Banker demo

( cd backend && uvicorn backend.main:app --reload & )
( cd frontend && npm run dev & )
wait
