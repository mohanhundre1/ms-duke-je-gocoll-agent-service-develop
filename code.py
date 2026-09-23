import os

# 1. Define the base directory
base_dir = os.path.join("agent", "src")

# 2. Define the subdirectories to create
dirs_to_create = [
    "agents",
    "assembly",
    "classify",
    "factories",
    "logic",
    "models",
    "output",
    "source_parsers",
    "validation",
    "workflow"
]

# 3. Create the subdirectories and add __init__.py to them
for d in dirs_to_create:
    full_dir_path = os.path.join(base_dir, d)
    os.makedirs(full_dir_path, exist_ok=True)
    
    # Create an __init__.py in each subfolder (standard Python package practice)
    init_path = os.path.join(full_dir_path, "__init__.py")
    with open(init_path, "w") as f:
        f.write('"""Placeholder for package."""\n')

# 4. Define the Python files to create directly in agent/src
files_to_create = [
    "__init__.py",
    "__main__.py",
    "gocoll_calculation_logic.py",
    "gocoll_explainability.py",
    "gocoll_pipeline.py",
    "observability.py",
    "serializers.py",
    "utils.py"
]

# 5. Create the files in agent/src with a placeholder
for filename in files_to_create:
    filepath = os.path.join(base_dir, filename)
    with open(filepath, "w") as f:
        f.write(f'"""Placeholder for {filename}."""\n')

print(f"Successfully created the '{base_dir}' folder structure.")