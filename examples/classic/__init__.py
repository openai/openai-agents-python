# Make the examples directory into a package to avoid top-level module name collisions.
# This is needed so that mypy treats files like examples/classic/customer_service/main.py and
# examples/classic/researcher_app/main.py as distinct modules rather than both named "main".
