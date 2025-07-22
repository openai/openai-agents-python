#!/bin/bash
# Launch backend and frontend for the demo.

(cd "$(dirname "$0")/backend" && uvicorn main:app --reload &)
(cd "$(dirname "$0")/frontend" && npm run dev)
