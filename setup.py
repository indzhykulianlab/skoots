import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    description="SKeletOn ObjecT Segmentation (SKOOTS)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=setuptools.find_packages(),
    python_requires=">=3.9",
    install_requires=["torch>=1.12.0", "torchvision>=0.13.0", "bism"],
    entry_points={
        "console_scripts": [
            "skoots-train = skoots.train.__main__:main",
            "skoots = skoots.__main__:main",
            "skoots-validate = skoots.validate.__main__:main",
        ]
    },
)
