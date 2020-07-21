import setuptools

name = "celavi"
version = "0.0.3"

with open("README.md", "r", encoding="utf8") as fh:
    long_description = fh.read()

setuptools.setup(
    name=name,
    version=version,
    author="Annika Eberle",
    author_email="annika.eberle@nrel.gov",
    description="Python model for the CELAVI project",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=["celavi"],
    install_requires=[
        "pytest",
        "mypy",
        "black",
        "sphinx",
        "pandas",
        "matplotlib",
        "numpy",
        "pysd",
        "networkx",
        "graphviz",
        "simpy",
    ]
)
