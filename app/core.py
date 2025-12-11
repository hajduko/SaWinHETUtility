import re
from collections import OrderedDict
from typing import Any, Dict, List
import base64
from datetime import datetime

# Split entries to 4 arrays based on types

def split_entries(entries):
    boundary = []
    mpd = []   # modernisationProposalDetails
    mps = []   # modernisationProposalsOfBuildingServicesSystems
    other = []

    for key, value in entries:
        if key.startswith("boundaryAndOpeningStructures__"):
            boundary.append([key, value])
        elif key.startswith("modernisationProposalDetails__"):
            mpd.append([key, value])
        elif key.startswith("modernisationProposalsOfBuildingServicesSystems__"):
            mps.append([key, value])
        else:
            other.append([key, value])

    return boundary, mpd, mps, other

# Normalize value

def normalize_value(raw: Any) -> Any:
    # If it's already a proper JSON type, don't touch it
    if isinstance(raw, (bool, int, float)) or raw is None:
        return raw

    # If it's not a string (e.g., list, dict), just return as is
    if not isinstance(raw, str):
        return raw

    # Now we know it's a string
    if raw == "TRUE":
        return True
    if raw == "FALSE":
        return False
    if raw == "null":
        return None

    s = raw.strip()
    if not s:
        return raw

    try:
        num = float(s)
        if re.fullmatch(r"-?\d+", s):
            return int(num)
        return num
    except ValueError:
        return raw

# Process boundary and opening structures

def process_boundary(entries):
    structures_by_type: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    for i in range(0, len(entries), 4):
        group = entries[i:i+4]

        if len(group) < 4:
            continue

        type_of_structure = normalize_value(group[0][1])
        quality = normalize_value(group[1][1])
        u = normalize_value(group[2][1])
        dimension = normalize_value(group[3][1])

        if quality is None or (isinstance(quality, str) and quality.strip() == ""):
            continue

        type_key = str(type_of_structure)
        if type_key not in structures_by_type:
            structures_by_type[type_key] = {
                "typeOfStructure": type_key,
                "energeticQualities": [],
            }

        structures_by_type[type_key]["energeticQualities"].append(
            {
                "quality": quality,
                "U": u,
                "dimension": dimension,
            }
        )
    
    return list(structures_by_type.values())

# Process modernisation proposal details

def process_mpd(entries):
    proposals = []

    for i in range(0, len(entries), 10):
        group = entries[i:i+10]

        if len(group) < 10:
            continue

        structure_type = normalize_value(group[0][1])
        structure_note = normalize_value(group[1][1])
        structure_surface_area = normalize_value(group[2][1])
        air_tightness = normalize_value(group[3][1])
        note = normalize_value(group[4][1])
        current_state_value = normalize_value(group[5][1])
        good_u = normalize_value(group[6][1])
        good_dimensions = normalize_value(group[7][1])
        excellent_u = normalize_value(group[8][1])
        excellent_dimensions = normalize_value(group[9][1])

        if structure_type is None or (isinstance(structure_type, str) and structure_type.strip() == ""):
            continue

        proposals.append({
            "structureType": structure_type,
            "structureNote": structure_note,
            "structureSurfaceArea": structure_surface_area,
            "airTightness": air_tightness,
            "note": note,
            "currentStateValue": current_state_value,
            "proposalEnergeticQualities": [
                {
                "quality": 'good',
                "u": good_u,
                "dimension": good_dimensions,
                },
                {
                "quality": 'excellent',
                "u": excellent_u,
                "dimension": excellent_dimensions,
                },
            ]
        })

    return proposals

# Process modernisation proposals of building services systems

def process_mps(entries):
    systems: List[Dict[str, Any]] = []

    current_system: Dict[str, Any] | None = None
    current_actual: Dict[str, Any] | None = None

    # Per-system grouping of recommendedModernisations by category
    # key: category string or None (for empty/missing category)
    recommended_by_cat: OrderedDict[Any, Dict[str, Any]] | None = None

    current_recommended: Dict[str, Any] | None = None
    current_element: Dict[str, Any] | None = None

    def get_or_create_recommended(cat_value):
        """Return the recommendedModernisation object for a category, creating if needed."""
        nonlocal recommended_by_cat, current_recommended

        if recommended_by_cat is None:
            recommended_by_cat = OrderedDict()

        # Normalize category: empty string or None => key None (category-less group)
        if cat_value in ("", None):
            key = None
        else:
            key = cat_value

        rec = recommended_by_cat.get(key)
        if rec is None:
            if key is None:
                rec = {"systemElements": []}
            else:
                rec = {"modernisationCategory": cat_value, "systemElements": []}
            recommended_by_cat[key] = rec

        current_recommended = rec
        return rec

    def finalize_element():
        nonlocal current_element, current_recommended
        if current_element is None:
            return
        if current_recommended is None:
            # No category yet -> treat as category-less group
            get_or_create_recommended(None)
        current_recommended.setdefault("systemElements", []).append(current_element)
        current_element = None

    def finalize_system():
        nonlocal current_system, current_actual, recommended_by_cat, current_element, current_recommended

        if current_system is None:
            return

        # Flush last element into its recommended group
        finalize_element()

        # Attach actualEnergeticQuality
        if current_actual is not None:
            q = current_actual.get("quality")
            n = current_actual.get("note")
            if q == "":
                current_system["actualEnergeticQuality"] = None
            else:
                current_system["actualEnergeticQuality"] = current_actual

        # Attach recommendedModernisations grouped by category
        if recommended_by_cat:
            current_system["recommendedModernisations"] = list(recommended_by_cat.values())

        systems.append(current_system)

        # Reset per-system state
        current_system = None
        current_actual = None
        recommended_by_cat = None
        current_recommended = None
        current_element = None

    # ---------------- main loop ----------------
    for flat_key, raw_value in entries:
        value = normalize_value(raw_value)

        prefix = "modernisationProposalsOfBuildingServicesSystems__"
        _, suffix = flat_key.split(prefix, 1)
        suffix = suffix.lstrip("_")
        parts = suffix.split("__")

        # ---- buildingServiceSystemType: new system ----
        if parts[0] == "buildingServiceSystemType":
            finalize_system()
            current_system = {"buildingServiceSystemType": value}
            current_actual = None
            recommended_by_cat = None
            current_recommended = None
            current_element = None
            continue

        # ---- top-level note ----
        if parts[0] == "note" and len(parts) == 1:
            if current_system is None:
                current_system = {}
            current_system["note"] = value
            continue

        # ---- actualEnergeticQuality ----
        if parts[0] == "actualEnergeticQuality" and len(parts) == 2:
            field = parts[1]  # "quality" or "note"
            if current_actual is None:
                current_actual = {}
            current_actual[field] = value
            continue

        # ---- recommendedModernisations ----
        if parts[0] == "recommendedModernisations":
            # modernisationCategory: select or create the group
            if len(parts) == 2 and parts[1] == "modernisationCategory":
                # finishing any open element before switching category
                finalize_element()
                get_or_create_recommended(value)
                continue

            # systemElements__...
            if len(parts) == 3 and parts[1] == "systemElements":
                field = parts[2]  # "name", "description", "isExcellentLevel"

                if field == "name":
                    # New element: flush previous element first
                    finalize_element()
                    current_element = {"name": value}
                elif field == "description":
                    if current_element is None:
                        current_element = {}
                    current_element["description"] = value
                elif field == "isExcellentLevel":
                    if current_element is None:
                        current_element = {}
                    current_element["isExcellentLevel"] = value
                continue

        # anything else is ignored

    # finalize last system
    finalize_system()

    return systems


# Process others

def process_other(entries):
    result: Dict[str, Any] = {}

    alternative_energies: List[str] = []

    for flat_key, raw_value in entries:
        value = normalize_value(raw_value)

        # --- SPECIAL CASE: usingAlternativeEnergy.alternativeEnergies ---
        if flat_key.startswith("usingAlternativeEnergy__alternativeEnergies__"):
            # Example key: "usingAlternativeEnergy__alternativeEnergies__HeatPump"
            # We only add names where value is TRUE
            if value is True:
                name = flat_key.split("__")[-1]
                alternative_energies.append(name)
            # Skip generic processing for these keys
            continue

        # Skip empty values entirely
        if value in ("", None):
            continue

        keys = flat_key.split("__")

        current = result
        for idx, key in enumerate(keys):
            is_last = idx == len(keys) - 1

            if is_last:
                arr_match = re.match(r"(.+)_\d+$", key)
                if arr_match:
                    arr_key = arr_match.group(1)
                    if arr_key not in current or not isinstance(current[arr_key], list):
                        current[arr_key] = []
                    current[arr_key].append(value)
                else:
                    current[key] = value
            else:
                if key not in current or not isinstance(current[key], dict):
                    current[key] = {}
                current = current[key]
    
    # After processing all entries, attach the collected alternativeEnergies
    if alternative_energies:
        ua = result.setdefault("usingAlternativeEnergy", {})
        ua["alternativeEnergies"] = alternative_energies

    return result

# Main process

def transform_json(
    input_obj: Dict[str, Any],
    pdf_bytes: bytes,
    images: List[Dict[str, Any]],   # each: {"content": bytes, "note": str, "category": str}
) -> Dict[str, Any]:
    """
    Pure-function version of the transformer.

    - input_obj: parsed JSON of the "pairs" file
    - pdf_bytes: raw bytes of the calculations PDF
    - images: list of dicts with keys:
        - "content": bytes
        - "note": str
        - "category": one of the allowed categories
    """
    entries = input_obj.get("data", [])

    boundary, mpd, mps, other = split_entries(entries)

    boundary_processed = process_boundary(boundary)
    mpd_processed = process_mpd(mpd)
    mps_processed = process_mps(mps)

    result = process_other(other)

    if (len(boundary_processed) > 0):
        result["boundaryAndOpeningStructures"] = boundary_processed
    
    if (len(mpd_processed) > 0):
        result["modernisationProposalDetails"] = mpd_processed

    if (len(mps_processed) > 0):
        result["modernisationProposalsOfBuildingServicesSystems"] = mps_processed

    # Force some numeric-ish address fields to strings
    result["buildingData"]["buildingAddress"]["houseNumber"] = str(
        result["buildingData"]["buildingAddress"]["houseNumber"]
    )
    result["buildingData"]["buildingAddress"]["building"] = str(
        result["buildingData"]["buildingAddress"]["building"]
    )
    result["buildingData"]["buildingAddress"]["floor"] = str(
        result["buildingData"]["buildingAddress"]["floor"]
    )
    result["buildingData"]["buildingAddress"]["doorNumber"] = str(
        result["buildingData"]["buildingAddress"]["doorNumber"]
    )
    result["buildingData"]["buildingAddress"]["staircase"] = str(
        result["buildingData"]["buildingAddress"]["staircase"]
    )
    result["buildingData"]["topographicalNumber"] = str(
        result["buildingData"]["topographicalNumber"]
    )

    result["certifierDetails"]["address"]["houseNumber"] = str(
        result["certifierDetails"]["address"]["houseNumber"]
    )
    result["certifierDetails"]["address"]["building"] = str(
        result["certifierDetails"]["address"]["building"]
    )
    result["certifierDetails"]["address"]["floor"] = str(
        result["certifierDetails"]["address"]["floor"]
    )
    result["certifierDetails"]["address"]["doorNumber"] = str(
        result["certifierDetails"]["address"]["doorNumber"]
    )
    result["certifierDetails"]["address"]["staircase"] = str(
        result["certifierDetails"]["address"]["staircase"]
    )
    result["certifierDetails"]["topographicalNumber"] = str(
        result["certifierDetails"]["topographicalNumber"]
    )
    result["certifierDetails"]["phoneNumber"] = "+" + str(
        result["certifierDetails"]["phoneNumber"]
    )

    result["validity"]["siteInspectionDate"] = datetime.strptime(result["validity"]["siteInspectionDate"], "%m/%d/%y").strftime("%y.%m.%d.")

    # --- PDF -> base64 ---
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    result["calculationsPdfFileContent"] = pdf_b64

    # --- Images -> photos (use category provided from frontend) ---
    photos = []
    for img in images:
        raw = img["content"]
        note = img.get("note", "")
        category = img.get("category")

        img_b64 = base64.b64encode(raw).decode("ascii")

        photos.append(
            {
                "category": category,
                "note": note,
                "content": img_b64,
            }
        )

    result["photos"] = photos

    return result