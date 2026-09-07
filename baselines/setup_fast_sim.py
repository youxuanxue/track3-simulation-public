"""Build the Cython OrderBook / Kernel extension in-place.

Usage (from the directory that contains ``fast_sim/``)::

    python setup_fast_sim.py build_ext --inplace
"""

from setuptools import Extension, setup

from Cython.Build import cythonize

setup(
    name="fast_sim_hotpath",
    ext_modules=cythonize(
        [
            Extension(
                "fast_sim._hotpath",
                ["fast_sim/_hotpath.pyx"],
                extra_compile_args=["-O3"],
            ),
            Extension(
                "fast_sim._native",
                ["fast_sim/_native.pyx"],
                extra_compile_args=["-O3"],
            ),
        ],
        language_level="3",
        compiler_directives={
            "boundscheck": False,
            "wraparound": True,
        },
    ),
)
