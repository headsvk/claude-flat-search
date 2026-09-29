"""Postcode district -> the neighbourhood a Londoner would name.

"42, 1 Water Lane NW1" says nothing to someone who does not carry the postcode
map in their head; "Camden Town / Regent's Park" does. A static table rather
than a geocoder: tried on 2026-09-28, OpenStreetMap's street lookup found
nothing for 3 of 8 sample addresses and put an SW11 listing in SW8. A district
never misplaces a flat; it is only coarse, so each entry names at most the two
places most of that district is known as.

A sub-district (W1K, SW1X, EC4Y) falls back to its parent when it has no entry
of its own.
"""
import re

AREAS = {
    # West End and the City
    "W1": "Mayfair / Marylebone", "W1B": "Soho / Regent St", "W1C": "Marylebone / Oxford St",
    "W1D": "Soho", "W1F": "Soho", "W1G": "Marylebone", "W1H": "Marylebone",
    "W1J": "Mayfair / St James's", "W1K": "Mayfair", "W1S": "Mayfair",
    "W1T": "Fitzrovia", "W1U": "Marylebone", "W1W": "Fitzrovia",
    "SW1": "Westminster / Belgravia", "SW1A": "St James's / Westminster", "SW1E": "Victoria",
    "SW1H": "Westminster", "SW1P": "Westminster", "SW1V": "Pimlico",
    "SW1W": "Belgravia / Victoria", "SW1X": "Belgravia / Knightsbridge", "SW1Y": "St James's",
    "WC1": "Bloomsbury / Holborn", "WC1A": "Bloomsbury", "WC1B": "Bloomsbury",
    "WC1E": "Bloomsbury", "WC1H": "Bloomsbury / St Pancras", "WC1N": "Bloomsbury",
    "WC1R": "Holborn", "WC1V": "Holborn", "WC1X": "King's Cross / Clerkenwell",
    "WC2": "Covent Garden / Strand", "WC2A": "Holborn / Lincoln's Inn", "WC2B": "Covent Garden",
    "WC2E": "Covent Garden", "WC2H": "Covent Garden / Leicester Sq", "WC2N": "Charing Cross / Strand",
    "WC2R": "Strand",
    "EC1": "Clerkenwell / Farringdon", "EC2": "City / Liverpool St",
    "EC3": "City / Aldgate", "EC4": "City / St Paul's",
    "EC4A": "Fleet St / Chancery Lane", "EC4M": "St Paul's", "EC4N": "Bank / Cannon St",
    "EC4R": "Cannon St", "EC4V": "Blackfriars", "EC4Y": "Temple / Fleet St",
    # West
    "W2": "Bayswater / Paddington", "W3": "Acton", "W4": "Chiswick", "W5": "Ealing",
    "W6": "Hammersmith", "W7": "Hanwell", "W8": "Kensington", "W9": "Maida Vale",
    "W10": "North Kensington / Ladbroke Grove", "W11": "Notting Hill / Holland Park",
    "W12": "Shepherd's Bush", "W13": "West Ealing", "W14": "West Kensington / Olympia",
    # South-west
    "SW3": "Chelsea", "SW4": "Clapham", "SW5": "Earl's Court", "SW6": "Fulham",
    "SW7": "South Kensington", "SW8": "Vauxhall / Nine Elms", "SW9": "Stockwell / Brixton",
    "SW10": "West Chelsea / West Brompton", "SW11": "Battersea / Nine Elms",
    "SW12": "Balham", "SW13": "Barnes", "SW14": "East Sheen / Mortlake",
    "SW15": "Putney", "SW16": "Streatham", "SW17": "Tooting", "SW18": "Wandsworth / Earlsfield",
    "SW19": "Wimbledon", "SW20": "Raynes Park",
    "TW1": "Twickenham / St Margarets", "TW2": "Twickenham / Whitton",
    "TW9": "Richmond / Kew", "TW10": "Richmond Hill / Ham", "TW11": "Teddington",
    "KT1": "Kingston", "KT2": "Kingston / Norbiton", "KT3": "New Malden",
    "KT5": "Surbiton / Berrylands", "KT6": "Surbiton",
    # North and north-west
    "NW1": "Camden Town / Regent's Park", "NW2": "Cricklewood", "NW3": "Hampstead / Belsize Park",
    "NW5": "Kentish Town", "NW6": "West Hampstead / Kilburn", "NW8": "St John's Wood",
    "NW10": "Willesden / Kensal Rise", "NW11": "Golders Green / Hampstead Garden Suburb",
    "N1": "Islington / Canonbury", "N1C": "King's Cross", "N2": "East Finchley",
    "N3": "Finchley", "N4": "Finsbury Park", "N5": "Highbury", "N6": "Highgate",
    "N7": "Holloway", "N8": "Crouch End", "N10": "Muswell Hill", "N16": "Stoke Newington",
    "N19": "Archway", "EN5": "Barnet",
    # East
    "E1": "Whitechapel / Aldgate", "E1W": "Wapping", "E2": "Bethnal Green", "E3": "Bow",
    "E8": "Hackney", "E9": "Hackney / Victoria Park", "E14": "Canary Wharf / Isle of Dogs",
    "E15": "Stratford", "E16": "Royal Docks / Canning Town", "E20": "Stratford / Olympic Park",
    "IG6": "Barkingside", "IG7": "Chigwell", "IG8": "Woodford Green",
    # South-east
    "SE1": "South Bank / Bermondsey", "SE3": "Blackheath", "SE5": "Camberwell",
    "SE8": "Deptford", "SE10": "Greenwich", "SE11": "Kennington", "SE15": "Peckham",
    "SE16": "Rotherhithe / Surrey Quays", "SE21": "Dulwich", "SE22": "East Dulwich",
    "SE24": "Herne Hill",
}


def area_name(district: str | None) -> str | None:
    if not district:
        return None
    district = district.upper()
    if district in AREAS:
        return AREAS[district]
    parent = re.sub(r"[A-Z]$", "", district)
    return AREAS.get(parent) if parent != district else None


def label(address: str, district: str | None) -> str | None:
    """The name to show next to `address`, or None when the address already
    says it - "Ruston Mews, Notting Hill W11" needs no "Notting Hill" after it."""
    name = area_name(district)
    if not name:
        return None
    said = address.lower()
    if any(part.strip().lower() in said for part in name.split("/")):
        return None
    return name
