# Basic Chainlit Deployment on Hugging Face

This guide outlines the process of deploying a Chainlit application using Docker on Hugging Face Spaces.

## Project Structure

```
01_basic_huggingface_deploy/
├── app.py              # Main application file
├── pyproject.toml      # Python project dependencies
├── Dockerfile          # Docker configuration
├── .env.example        # Example environment variables
├── .dockerignore       # Docker ignore file
└── chainlit.md         # Chainlit welcome page
```

## Deployment Steps

### 1. Create a New Space on Hugging Face

1. Go to [Hugging Face Spaces](https://huggingface.co/spaces)
2. Click on "Create new Space"
3. Choose "Docker" as the SDK
4. Name your space and set it to "Public"
5. Create the space
6. Set up environment variables in Hugging Face:
   - Go to your Space's Settings
   - Add `GEMINI_API_KEY` in the "Repository Secrets" section
7. **Important**: After creation, drag and drop these 3 files to your space:
   - `Dockerfile`
   - `app.py`
   - `pyproject.toml`
8. The Space will automatically build and deploy your application


### 2. Local Development Setup

1. Install UV package manager

2. Create a virtual environment:
   ```bash
   uv venv
   .venv\Scripts\activate  # On Mac: source .venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   uv sync
   ```

4. Create a `.env` file from the example:
   ```bash
   cp .env.example .env
   ```

5. Add your API key to `.env`:
   ```
   GEMINI_API_KEY=your-api-key
   ```

6. Run the application locally:
   ```bash
   uv run chainlit run app.py
   ```

### 3. Docker Setup

1. Build the Docker image:
   ```bash
   docker build -t chainlit-app .
   ```

2. Run the Docker container:
   ```bash
   docker run -p 7860:7860 -e GEMINI_API_KEY=your-api-key chainlit-app
   ```

## Features

- Gemini AI integration
- Docker containerization
- UV package management
- Secure environment variable handling

## Notes

- The application runs on port 7860 by default
- Make sure to keep your API key secure and never commit it to version control
- The Docker setup uses a non-root user for better security
- UV is used for efficient Python package management

## Troubleshooting

- If the Space is stuck in "Building", check the build logs
- Ensure all required files are present in the Space
- Verify that the environment variables are set correctly
- Check if the port 7860 is exposed correctly