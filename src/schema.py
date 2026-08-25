"""
DEPRECATED compatibility shim -- the implementation moved to src/utilities/schema.py in C7.

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new module.

NOTE: this is a pure relocation. The degree-level taxonomy change
(undergraduate/graduate/postgraduate_and_phd -> Bachelors/Masters/PhD/Diploma) and
the per-program required-field additions are deliberately deferred to C17 and C18,
so that a move commit never doubles as a behaviour change.
"""
from src.utilities.schema import *  # noqa: F401,F403
from src.utilities.schema import (  # noqa: F401
    ApplicationStatus,
    ContactInfo,
    DegreeLevel,
    EligibilityRequirements,
    FacultyItem,
    KeyLinks,
    MainInfo,
    ProgramCategoryBlock,
    ProgramItem,
    RankingItem,
    SubCampusContact,
    UniversityPayload,
    UniversityType,
)
