# Contributing Guidelines

The following is a set of guidelines for contributing to this project. We really appreciate that you are considering contributing!

#### Table Of Contents

[Getting Started](#getting-started)

[Contributing](#contributing)

[Code Guidelines](#code-guidelines)

[Code of Conduct](https://github.com/nginxinc/nginx-otel/blob/main/CODE_OF_CONDUCT.md)

## Getting Started

Follow our [Getting Started Guide](https://github.com/nginxinc/nginx-otel/blob/main/README.md) to get this project up and running.

<!-- ### Project Structure (OPTIONAL) -->

## Contributing

### Report a Bug

To report a bug, open an issue on GitHub with the label `bug` using the available bug report issue template. Please ensure the bug has not already been reported. **If the bug is a potential security vulnerability, please report it using our [security policy](https://github.com/nginxinc/nginx-otel/blob/main/SECURITY.md).**

### Suggest a Feature or Enhancement

To suggest a new feature or other improvement, create an issue on GitHub and choose the type 'Feature request'. Please fill in the template as provided.

### Open a Pull Request

- Fork the repo, create a branch, implement your changes, add any relevant tests, submit a PR when your changes are **tested** and ready for review.
- Fill in [our pull request template](https://github.com/nginxinc/nginx-otel/blob/main/.github/pull_request_template.md).

## Code Guidelines

### NGINX Code Guidelines

Before diving into the NGINX codebase or contributing, it's important to understand the fundamental principles and techniques outlined in the [NGINX Development Guide] (http://nginx.org/en/docs/dev/development_guide.html).

### Git Guidelines

- Keep a clean, concise and meaningful git commit history on your branch (within reason), rebasing locally and squashing before submitting a PR.
- Follow below guidelines for writing commit messages:
  - In the subject line, use the present tense ("Add feature" not "Added feature").
  - In the subject line, use the imperative mood ("Move cursor to..." not "Moves cursor to...").
  - End subject line with a period.
  - Limit the subject line to 72 characters or less.
  - Reference issues in the subject line and/or body.
  - Add more detailed description in the body of the git message (`git commit -a` to give you more space and time in your text editor to write a good message instead of `git commit -am`).
