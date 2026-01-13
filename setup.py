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

    def egg2dist(self, egginfo_dir: str, distinfo_dir: str) -> None:
        if os.path.exists(distinfo_dir):
            shutil.rmtree(distinfo_dir)
        shutil.copytree(egginfo_dir, distinfo_dir)
        pkg_info = os.path.join(distinfo_dir, "PKG-INFO")
        metadata = os.path.join(distinfo_dir, "METADATA")
        if os.path.exists(pkg_info):
            shutil.copyfile(pkg_info, metadata)
        if not os.path.exists(metadata):
            with open(metadata, "w", encoding="utf-8") as handle:
                handle.write(
                    "Metadata-Version: 2.1\n"
                    "Name: kitchen-scanner\n"
                    "Version: 0.1.0\n"
                )
        wheel_file = os.path.join(distinfo_dir, "WHEEL")
        if not os.path.exists(wheel_file):
            with open(wheel_file, "w", encoding="utf-8") as handle:
                handle.write(
                    "Wheel-Version: 1.0\n"
                    "Generator: fallback-bdist-wheel\n"
                    "Root-Is-Purelib: true\n"
                    "Tag: py3-none-any\n"
                )

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
