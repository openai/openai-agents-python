# Contributing to OpenAI Agents SDK

Thank you for your interest in contributing to the OpenAI Agents SDK! This document provides guidelines and instructions to help you contribute effectively.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
  - [Development Environment Setup](#development-environment-setup)
  - [Installing Dependencies](#installing-dependencies)
- [How to Contribute](#how-to-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Enhancements](#suggesting-enhancements)
  - [Pull Requests](#pull-requests)
- [Development Guidelines](#development-guidelines)
  - [Code Style](#code-style)
  - [Testing](#testing)
  - [Documentation](#documentation)
- [Commit Guidelines](#commit-guidelines)
- [Community](#community)

## Code of Conduct

We are committed to providing a friendly, safe, and welcoming environment for all contributors. Please read and adhere to our [Code of Conduct](CODE_OF_CONDUCT.md) in all your interactions with the project.

## Getting Started

### Development Environment Setup

1. **Fork the repository** and clone your fork:

   ```bash
   git clone https://github.com/YOUR_USERNAME/openai-agents-python.git
   cd openai-agents-python
   ```

2. **Add the original repository as an upstream remote** to keep your fork in sync:

   ```bash
   git remote add upstream https://github.com/openai/openai-agents-python.git
   ```

### Installing Dependencies

1. **Ensure you have [`uv`](https://docs.astral.sh/uv/) installed**:

   ```bash
   uv --version
   ```

2. **Install development dependencies**:

   ```bash
   make sync
   ```

## How to Contribute

### Reporting Bugs

Before submitting a bug report:

1. Check the [issue tracker](https://github.com/openai/openai-agents-python/issues) to avoid duplicates
2. Update to the latest version to see if the issue persists

When submitting a bug report, please include:

- A clear, descriptive title
- Steps to reproduce the issue
- Expected behavior and actual behavior
- Environment details (OS, Python version, library versions)
- Any relevant error messages or logs

### Suggesting Enhancements

Enhancement suggestions are always welcome! Please provide:

- A clear description of the enhancement
- The motivation behind it
- Possible implementation approaches if you have any in mind
- Any relevant examples or use cases

### Pull Requests

To submit a pull request:

1. **Create a new branch** from the main branch:

   ```bash
   git checkout main
   git pull upstream main
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** and commit them following the [commit guidelines](#commit-guidelines)

3. **Push your branch** to your fork:

   ```bash
   git push origin feature/your-feature-name
   ```

4. **Submit a pull request** to the main branch of the original repository

5. **Update your pull request** based on review feedback

## Development Guidelines

### Code Style

We follow the Python community's style conventions:

- Use [Ruff](https://github.com/astral-sh/ruff) for linting
- Follow [PEP 8](https://peps.python.org/pep-0008/) style guidelines
- Use [MyPy](https://mypy.readthedocs.io/en/stable/) for type checking

To check your code:

```bash
make lint  # Run linter
make mypy  # Run type checker
```

### Testing

All new features and bug fixes should include tests. We use Python's standard `unittest` framework.

To run tests:

```bash
make tests
```

Please ensure all tests pass before submitting a pull request.

### Documentation

Good documentation is essential for our project:

- Update relevant documentation for any changes you make
- Use clear docstrings for all functions, classes, and methods
- Follow [Google style docstrings](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings)
- Add examples for new features when appropriate

## Commit Guidelines

We follow conventional commits to make the review process easier and maintain a clear history:

- Use a concise summary in the present tense
- Reference issues and pull requests when relevant
- Structure your commits logically (one feature/fix per commit)

Examples:

- `feat: add support for custom tool validation`
- `fix: resolve handoff loop issue`
- `docs: update tracing documentation`
- `test: add tests for guardrails functionality`

## Community

- **Issue Tracker**: [GitHub Issues](https://github.com/openai/openai-agents-python/issues) for bugs and feature requests
- **Documentation**: Refer to our [documentation](https://openai.github.io/openai-agents-python/) for detailed guides

---

Thank you for contributing to the OpenAI Agents SDK! Your time and expertise help make this project better for everyone.
