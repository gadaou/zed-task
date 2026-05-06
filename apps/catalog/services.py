"""Catalog services.

Product lookup and stock validation live here. Called by cart services when
adding items and re-called at checkout to detect price changes and stock
depletion (PROJECT_SPEC §8).
"""
