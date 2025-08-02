
# Release process/changelog

The project uses a versioning system in the form of `0.Y.Z`. The starting `0` means the SDK is still in a fast-evolving stage. The numbers `Y` and `Z` are incremented as follows:

## Minor (`Y`) versions

We increase `Y` for **major, breaking changes** to public parts of the code. This means the new version might cause your existing code to fail. For example, changing from `0.0.x` to `0.1.x` could include breaking changes.

> **Tip:** To avoid breaking changes, pin your project to a specific minor version, like `0.0.x`.

## Patch (`Z`) versions

We increase `Z` for **minor, non-breaking changes** that will not affect your existing code. These include:

-   Bug fixes
    
-   New features
    
-   Changes to internal code
    
-   Updates to beta features
    

## Breaking change changelog

### 0.2.0

In this version, some functions that used to take `Agent` as an argument now take `AgentBase`. This is a typing change, so to fix any type errors, simply replace `Agent` with `AgentBase` in your code.

### 0.1.0

In this version, `MCPServer.list_tools()` now has two new parameters: `run_context` and `agent`. If you have a class that inherits from `MCPServer`, you must add these two new parameters to your `list_tools()` method.
