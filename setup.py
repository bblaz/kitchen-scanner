from setuptools import Command, find_packages, setup


class BDistWheel(Command):
    """Fallback bdist_wheel command for environments without wheel installed."""

    user_options = []

    def initialize_options(self) -> None:
        pass

    def finalize_options(self) -> None:
        pass

    def run(self) -> None:
        pass

setup(
    name="kitchen-scanner",
    version="0.1.0",
    description="Kitchen barcode scanner service",
    author="Blaž Bregar",
    author_email="blaz@aklaro.si",
    packages=find_packages(),
    py_modules=["main"],
    install_requires=[
        "evdev",
        "httpx",
        "tenacity",
    ],
    python_requires=">=3.9",
    cmdclass={"bdist_wheel": BDistWheel},
)
