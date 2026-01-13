import os
import shutil

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

    @staticmethod
    def egg2dist(egginfo_dir: str, distinfo_dir: str) -> None:
        if os.path.exists(distinfo_dir):
            shutil.rmtree(distinfo_dir)
        shutil.copytree(egginfo_dir, distinfo_dir)

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
