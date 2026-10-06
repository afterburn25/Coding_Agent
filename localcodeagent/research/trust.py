"""Source trust registry and topic-aware authority.

Distinguishes SOURCE REPUTATION from EVIDENCE QUALITY:

- A domain profile says what a site generally *is* (class, editorial
  control, user-generated flag) and which topics it is authoritative FOR.
- The same site earns different authority for different questions —
  docs.python.org is primary for Python behavior, irrelevant for gumbo.
- github.com is not a monolith: python/cpython is an official repository;
  a random fork is a community repo. Repo identity is resolved per-URL.

The registry is prior knowledge, not a hard whitelist: evidence scoring
in evidence.py still weighs freshness, version match, independence and
corroboration on top of these priors. User-configured trusted/blocked
domains remain overrides applied by the ranker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Source classes (Part 3)
# ---------------------------------------------------------------------------
PRIMARY_OFFICIAL = "primary_official"
OFFICIAL_DOCUMENTATION = "official_documentation"
GOVERNMENT = "government"
STANDARD_BODY = "standard_body"
PEER_REVIEWED_RESEARCH = "peer_reviewed_research"
ACADEMIC_INSTITUTION = "academic_institution"
ORIGINAL_PROJECT = "original_project"
ORIGINAL_DATA = "original_data"
MAJOR_PROFESSIONAL_SOURCE = "major_professional_source"
REPUTABLE_SECONDARY = "reputable_secondary"
NEWS_PRIMARY_REPORTING = "news_primary_reporting"
SPECIALIST_PUBLICATION = "specialist_publication"
COMMUNITY_HIGH_SIGNAL = "community_high_signal"
FORUM = "forum"
BLOG = "blog"
AGGREGATOR = "aggregator"
SCRAPED_CONTENT = "scraped_content"
SEO_CONTENT = "seo_content"
UNKNOWN = "unknown"

CLASS_LABEL = {
    PRIMARY_OFFICIAL: "PRIMARY",
    OFFICIAL_DOCUMENTATION: "OFFICIAL",
    GOVERNMENT: "GOVERNMENT",
    STANDARD_BODY: "STANDARDS",
    PEER_REVIEWED_RESEARCH: "PEER REVIEWED",
    ACADEMIC_INSTITUTION: "ACADEMIC",
    ORIGINAL_PROJECT: "PROJECT",
    ORIGINAL_DATA: "PRIMARY DATA",
    MAJOR_PROFESSIONAL_SOURCE: "PROFESSIONAL",
    REPUTABLE_SECONDARY: "SECONDARY",
    NEWS_PRIMARY_REPORTING: "NEWS",
    SPECIALIST_PUBLICATION: "SPECIALIST",
    COMMUNITY_HIGH_SIGNAL: "COMMUNITY",
    FORUM: "FORUM",
    BLOG: "BLOG",
    AGGREGATOR: "AGGREGATOR",
    SCRAPED_CONTENT: "SCRAPED",
    SEO_CONTENT: "SEO",
    UNKNOWN: "UNKNOWN",
}

# ---------------------------------------------------------------------------
# Topic categories (Part 5)
# ---------------------------------------------------------------------------
SOFTWARE = "software"
PROGRAMMING = "programming"
CYBERSECURITY = "cybersecurity"
MEDICAL = "medical"
HEALTH = "health"
LEGAL = "legal"
SCIENCE = "science"
ACADEMIC = "academic"
FINANCE = "finance"
ECONOMICS = "economics"
NEWS = "news"
POLITICS = "politics"
PRODUCT_SPECS = "product_specs"
CONSUMER_REVIEW = "consumer_review"
COOKING = "cooking"
HOME = "home"
AUTOMOTIVE = "automotive"
TRAVEL = "travel"
HISTORY = "history"
GENERAL = "general"

HIGH_STAKES_TOPICS = {MEDICAL, HEALTH, LEGAL, FINANCE}


@dataclass
class SourceTrustProfile:
    domain: str
    category: str = UNKNOWN
    base_authority: float = 30.0
    primary_source: bool = False
    editorial_control: bool = False
    community_source: bool = False
    user_generated: bool = False
    official_for: list[str] = field(default_factory=list)   # topic ids
    preferred_topics: list[str] = field(default_factory=list)
    discouraged_topics: list[str] = field(default_factory=list)
    freshness_expectation: str = "medium"  # low|medium|high
    preprint: bool = False
    notes: str = ""
    version: int = 1  # registry schema version for updateability

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "category": self.category,
            "base_authority": self.base_authority,
            "primary_source": self.primary_source,
            "editorial_control": self.editorial_control,
            "community_source": self.community_source,
            "user_generated": self.user_generated,
            "official_for": list(self.official_for),
            "preferred_topics": list(self.preferred_topics),
            "discouraged_topics": list(self.discouraged_topics),
            "freshness_expectation": self.freshness_expectation,
            "preprint": self.preprint,
            "notes": self.notes,
        }


def _p(domain: str, category: str, authority: float, **kw: Any) -> SourceTrustProfile:
    return SourceTrustProfile(domain=domain, category=category,
                              base_authority=float(authority), **kw)


# Built-in seed registry — prior knowledge, deliberately not exhaustive.
# Topic-aware: `official_for`/`preferred_topics` list where the site is
# authoritative; `discouraged_topics` mark known-weak coverage.
_SEED: list[SourceTrustProfile] = [
    # --- software / development documentation --------------------------------
    _p("docs.python.org", OFFICIAL_DOCUMENTATION, 92, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING],
       preferred_topics=[SOFTWARE, PROGRAMMING], notes="Python language docs"),
    _p("developer.mozilla.org", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING],
       preferred_topics=[SOFTWARE, PROGRAMMING], notes="MDN web platform docs"),
    _p("learn.microsoft.com", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING],
       preferred_topics=[SOFTWARE, PROGRAMMING]),
    _p("docs.github.com", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("docs.nvidia.com", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING],
       preferred_topics=[SOFTWARE, PRODUCT_SPECS]),
    _p("developer.nvidia.com", OFFICIAL_DOCUMENTATION, 88, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("developer.apple.com", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("developers.google.com", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("docs.oracle.com", OFFICIAL_DOCUMENTATION, 88, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("doc.rust-lang.org", OFFICIAL_DOCUMENTATION, 92, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("rust-lang.org", ORIGINAL_PROJECT, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("go.dev", OFFICIAL_DOCUMENTATION, 92, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("nodejs.org", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("react.dev", OFFICIAL_DOCUMENTATION, 92, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("typescriptlang.org", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("cmake.org", OFFICIAL_DOCUMENTATION, 88, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("doc.qt.io", OFFICIAL_DOCUMENTATION, 88, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("pytorch.org", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("docs.comfy.org", OFFICIAL_DOCUMENTATION, 86, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("huggingface.co", SPECIALIST_PUBLICATION, 70, editorial_control=True,
       preferred_topics=[SOFTWARE, SCIENCE],
       notes="model hub — community uploads vary; official org repos differ"),
    _p("bfl.ai", OFFICIAL_DOCUMENTATION, 85, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE]),
    _p("qwenlm.github.io", OFFICIAL_DOCUMENTATION, 84, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE]),
    _p("kubernetes.io", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("docs.docker.com", OFFICIAL_DOCUMENTATION, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("gnu.org", OFFICIAL_DOCUMENTATION, 86, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("kernel.org", PRIMARY_OFFICIAL, 90, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    # --- standards bodies -----------------------------------------------------
    _p("w3.org", STANDARD_BODY, 92, primary_source=True, editorial_control=True,
       official_for=[SOFTWARE, PROGRAMMING]),
    _p("ietf.org", STANDARD_BODY, 92, primary_source=True, editorial_control=True,
       official_for=[SOFTWARE, PROGRAMMING]),
    _p("rfc-editor.org", STANDARD_BODY, 92, primary_source=True, editorial_control=True,
       official_for=[SOFTWARE, PROGRAMMING]),
    _p("ecma-international.org", STANDARD_BODY, 92, primary_source=True,
       editorial_control=True, official_for=[SOFTWARE, PROGRAMMING]),
    _p("iso.org", STANDARD_BODY, 90, primary_source=True, editorial_control=True,
       official_for=[SOFTWARE]),
    _p("khronos.org", STANDARD_BODY, 88, primary_source=True, editorial_control=True,
       official_for=[SOFTWARE, PROGRAMMING]),
    # --- community / forums ---------------------------------------------------
    _p("github.com", ORIGINAL_PROJECT, 60, community_source=True,
       preferred_topics=[SOFTWARE, PROGRAMMING],
       notes="per-repo: official org repos are primary; random forks are community"),
    _p("stackoverflow.com", COMMUNITY_HIGH_SIGNAL, 58, community_source=True,
       user_generated=True, preferred_topics=[SOFTWARE, PROGRAMMING],
       notes="practical troubleshooting; corroborate API contracts against docs"),
    _p("stackexchange.com", COMMUNITY_HIGH_SIGNAL, 52, community_source=True,
       user_generated=True, preferred_topics=[SOFTWARE, PROGRAMMING, SCIENCE]),
    _p("superuser.com", COMMUNITY_HIGH_SIGNAL, 52, community_source=True,
       user_generated=True, preferred_topics=[SOFTWARE, HOME]),
    _p("serverfault.com", COMMUNITY_HIGH_SIGNAL, 52, community_source=True,
       user_generated=True, preferred_topics=[SOFTWARE]),
    _p("reddit.com", FORUM, 34, community_source=True, user_generated=True,
       notes="experience signal only — never sole basis for factual claims"),
    _p("news.ycombinator.com", FORUM, 36, community_source=True, user_generated=True,
       preferred_topics=[SOFTWARE]),
    _p("quora.com", FORUM, 22, community_source=True, user_generated=True),
    _p("discuss.python.org", COMMUNITY_HIGH_SIGNAL, 60, community_source=True,
       user_generated=True, preferred_topics=[SOFTWARE, PROGRAMMING],
       notes="maintainers participate here"),
    # --- reference / secondary ------------------------------------------------
    _p("wikipedia.org", REPUTABLE_SECONDARY, 58, editorial_control=True,
       community_source=True, user_generated=True,
       discouraged_topics=[MEDICAL, LEGAL],
       notes="good orientation; cite primary sources for consequential claims"),
    _p("britannica.com", REPUTABLE_SECONDARY, 62, editorial_control=True,
       preferred_topics=[HISTORY, SCIENCE, GENERAL]),
    _p("wikihow.com", AGGREGATOR, 30, community_source=True, user_generated=True),
    _p("geeksforgeeks.org", REPUTABLE_SECONDARY, 48,
       preferred_topics=[SOFTWARE, PROGRAMMING],
       notes="secondary tutorials — verify APIs against official docs"),
    _p("w3schools.com", REPUTABLE_SECONDARY, 46,
       preferred_topics=[SOFTWARE, PROGRAMMING]),
    _p("tutorialspoint.com", AGGREGATOR, 38, preferred_topics=[SOFTWARE]),
    _p("medium.com", BLOG, 28, user_generated=True),
    _p("dev.to", BLOG, 32, community_source=True, user_generated=True,
       preferred_topics=[SOFTWARE]),
    _p("substack.com", BLOG, 30, user_generated=True),
    # --- health / medical ------------------------------------------------------
    _p("cdc.gov", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[MEDICAL, HEALTH], preferred_topics=[MEDICAL, HEALTH]),
    _p("nih.gov", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[MEDICAL, HEALTH, SCIENCE], preferred_topics=[MEDICAL, HEALTH, SCIENCE]),
    _p("fda.gov", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[MEDICAL, HEALTH], preferred_topics=[MEDICAL, HEALTH, PRODUCT_SPECS]),
    _p("who.int", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[MEDICAL, HEALTH], preferred_topics=[MEDICAL, HEALTH]),
    _p("nhs.uk", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[MEDICAL, HEALTH], preferred_topics=[MEDICAL, HEALTH]),
    _p("mayoclinic.org", MAJOR_PROFESSIONAL_SOURCE, 84, editorial_control=True,
       official_for=[HEALTH], preferred_topics=[MEDICAL, HEALTH],
       notes="major medical institution patient guidance"),
    _p("clevelandclinic.org", MAJOR_PROFESSIONAL_SOURCE, 82, editorial_control=True,
       official_for=[HEALTH], preferred_topics=[MEDICAL, HEALTH]),
    _p("hopkinsmedicine.org", MAJOR_PROFESSIONAL_SOURCE, 84, editorial_control=True,
       official_for=[MEDICAL, HEALTH], preferred_topics=[MEDICAL, HEALTH]),
    _p("webmd.com", REPUTABLE_SECONDARY, 50, editorial_control=True,
       preferred_topics=[HEALTH],
       notes="consumer secondary — corroborate with agencies/institutions"),
    _p("healthline.com", REPUTABLE_SECONDARY, 45, editorial_control=True,
       preferred_topics=[HEALTH]),
    _p("medlineplus.gov", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[MEDICAL, HEALTH], preferred_topics=[MEDICAL, HEALTH]),
    # --- science / academic -----------------------------------------------------
    _p("pubmed.ncbi.nlm.nih.gov", PEER_REVIEWED_RESEARCH, 90, primary_source=True,
       editorial_control=True, official_for=[SCIENCE, MEDICAL, ACADEMIC],
       preferred_topics=[SCIENCE, MEDICAL, HEALTH, ACADEMIC],
       notes="peer-reviewed literature index"),
    _p("nature.com", PEER_REVIEWED_RESEARCH, 88, editorial_control=True,
       official_for=[SCIENCE], preferred_topics=[SCIENCE, ACADEMIC, NEWS]),
    _p("science.org", PEER_REVIEWED_RESEARCH, 88, editorial_control=True,
       official_for=[SCIENCE], preferred_topics=[SCIENCE, ACADEMIC]),
    _p("arxiv.org", ORIGINAL_DATA, 55, preprint=True,
       preferred_topics=[SCIENCE, ACADEMIC, SOFTWARE],
       notes="PREPRINT — not peer reviewed; label accordingly"),
    _p("biorxiv.org", ORIGINAL_DATA, 52, preprint=True,
       preferred_topics=[SCIENCE, MEDICAL], notes="PREPRINT — not peer reviewed"),
    _p("medrxiv.org", ORIGINAL_DATA, 52, preprint=True,
       preferred_topics=[SCIENCE, MEDICAL], notes="PREPRINT — not peer reviewed"),
    _p("scholar.google.com", ORIGINAL_DATA, 65, preferred_topics=[SCIENCE, ACADEMIC]),
    _p("nasa.gov", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[SCIENCE], preferred_topics=[SCIENCE]),
    _p("usgs.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[SCIENCE], preferred_topics=[SCIENCE]),
    _p("noaa.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[SCIENCE], preferred_topics=[SCIENCE]),
    # --- legal / government ------------------------------------------------------
    _p("congress.gov", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[LEGAL], preferred_topics=[LEGAL, POLITICS]),
    _p("uscourts.gov", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[LEGAL], preferred_topics=[LEGAL]),
    _p("govinfo.gov", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[LEGAL], preferred_topics=[LEGAL]),
    _p("regulations.gov", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[LEGAL], preferred_topics=[LEGAL]),
    _p("supremecourt.gov", GOVERNMENT, 92, primary_source=True, editorial_control=True,
       official_for=[LEGAL], preferred_topics=[LEGAL]),
    _p("eur-lex.europa.eu", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[LEGAL], preferred_topics=[LEGAL], notes="EU law — jurisdiction matters"),
    _p("law.cornell.edu", ACADEMIC_INSTITUTION, 72, editorial_control=True,
       preferred_topics=[LEGAL], notes="LII — high-quality secondary, not primary law"),
    _p("findlaw.com", REPUTABLE_SECONDARY, 48, preferred_topics=[LEGAL]),
    _p("justia.com", REPUTABLE_SECONDARY, 50, preferred_topics=[LEGAL],
       notes="case text is primary material; commentary is secondary"),
    _p("sec.gov", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[FINANCE, LEGAL], preferred_topics=[FINANCE, LEGAL]),
    # --- news / current events ---------------------------------------------------
    _p("reuters.com", NEWS_PRIMARY_REPORTING, 76, editorial_control=True,
       preferred_topics=[NEWS, POLITICS, FINANCE, ECONOMICS],
       notes="wire service — copies on other sites are dependent, not corroborating"),
    _p("apnews.com", NEWS_PRIMARY_REPORTING, 76, editorial_control=True,
       preferred_topics=[NEWS, POLITICS],
       notes="wire service — copies on other sites are dependent, not corroborating"),
    _p("bbc.com", NEWS_PRIMARY_REPORTING, 72, editorial_control=True,
       preferred_topics=[NEWS, POLITICS, SCIENCE]),
    _p("bbc.co.uk", NEWS_PRIMARY_REPORTING, 72, editorial_control=True,
       preferred_topics=[NEWS, POLITICS, SCIENCE]),
    _p("nytimes.com", NEWS_PRIMARY_REPORTING, 72, editorial_control=True,
       preferred_topics=[NEWS, POLITICS, COOKING, CONSUMER_REVIEW]),
    _p("wsj.com", NEWS_PRIMARY_REPORTING, 72, editorial_control=True,
       preferred_topics=[NEWS, FINANCE, ECONOMICS]),
    _p("theguardian.com", NEWS_PRIMARY_REPORTING, 68, editorial_control=True,
       preferred_topics=[NEWS, POLITICS]),
    _p("npr.org", NEWS_PRIMARY_REPORTING, 70, editorial_control=True,
       preferred_topics=[NEWS, POLITICS]),
    _p("arstechnica.com", SPECIALIST_PUBLICATION, 64, editorial_control=True,
       preferred_topics=[SOFTWARE, SCIENCE, PRODUCT_SPECS]),
    _p("theverge.com", SPECIALIST_PUBLICATION, 58, editorial_control=True,
       preferred_topics=[SOFTWARE, PRODUCT_SPECS, CONSUMER_REVIEW]),
    _p("tomshardware.com", SPECIALIST_PUBLICATION, 60, editorial_control=True,
       preferred_topics=[PRODUCT_SPECS, SOFTWARE, CONSUMER_REVIEW]),
    _p("anandtech.com", SPECIALIST_PUBLICATION, 64, editorial_control=True,
       preferred_topics=[PRODUCT_SPECS, SOFTWARE]),
    # --- finance ------------------------------------------------------------------
    _p("federalreserve.gov", GOVERNMENT, 90, primary_source=True, editorial_control=True,
       official_for=[ECONOMICS, FINANCE], preferred_topics=[ECONOMICS, FINANCE]),
    _p("treasury.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[ECONOMICS, FINANCE], preferred_topics=[ECONOMICS, FINANCE]),
    _p("bls.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[ECONOMICS], preferred_topics=[ECONOMICS, NEWS]),
    _p("investopedia.com", REPUTABLE_SECONDARY, 50, editorial_control=True,
       preferred_topics=[FINANCE, ECONOMICS]),
    # --- cooking / food ------------------------------------------------------------
    _p("seriouseats.com", SPECIALIST_PUBLICATION, 72, editorial_control=True,
       official_for=[COOKING], preferred_topics=[COOKING],
       notes="tested recipes/technique — high practical authority for cooking"),
    _p("americastestkitchen.com", SPECIALIST_PUBLICATION, 72, editorial_control=True,
       official_for=[COOKING], preferred_topics=[COOKING]),
    _p("cooksillustrated.com", SPECIALIST_PUBLICATION, 72, editorial_control=True,
       official_for=[COOKING], preferred_topics=[COOKING]),
    _p("bonappetit.com", SPECIALIST_PUBLICATION, 66, editorial_control=True,
       preferred_topics=[COOKING]),
    _p("epicurious.com", SPECIALIST_PUBLICATION, 62, editorial_control=True,
       preferred_topics=[COOKING]),
    _p("foodnetwork.com", SPECIALIST_PUBLICATION, 58, editorial_control=True,
       preferred_topics=[COOKING]),
    _p("kingarthurbaking.com", SPECIALIST_PUBLICATION, 70, editorial_control=True,
       official_for=[COOKING], preferred_topics=[COOKING],
       notes="tested baking formulae"),
    _p("thekitchn.com", SPECIALIST_PUBLICATION, 56, editorial_control=True,
       preferred_topics=[COOKING, HOME]),
    _p("budgetbytes.com", SPECIALIST_PUBLICATION, 58, editorial_control=True,
       preferred_topics=[COOKING]),
    _p("allrecipes.com", COMMUNITY_HIGH_SIGNAL, 46, community_source=True,
       user_generated=True, preferred_topics=[COOKING],
       notes="community recipes — weigh ratings/sample size, not authority"),
    _p("food.com", COMMUNITY_HIGH_SIGNAL, 40, community_source=True,
       user_generated=True, preferred_topics=[COOKING]),
    _p("southernliving.com", SPECIALIST_PUBLICATION, 60, editorial_control=True,
       preferred_topics=[COOKING], notes="regional Southern cooking expertise"),
    _p("saveur.com", SPECIALIST_PUBLICATION, 64, editorial_control=True,
       preferred_topics=[COOKING]),
    # --- automotive / home ---------------------------------------------------------
    _p("nhtsa.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[AUTOMOTIVE], preferred_topics=[AUTOMOTIVE, PRODUCT_SPECS]),
    _p("energy.gov", GOVERNMENT, 86, primary_source=True, editorial_control=True,
       official_for=[HOME, PRODUCT_SPECS], preferred_topics=[HOME, SCIENCE]),
    _p("epa.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[AUTOMOTIVE, SCIENCE], preferred_topics=[AUTOMOTIVE, SCIENCE]),
    # --- travel ----------------------------------------------------------------------
    _p("travel.state.gov", GOVERNMENT, 88, primary_source=True, editorial_control=True,
       official_for=[TRAVEL], preferred_topics=[TRAVEL, LEGAL]),
    _p("lonelyplanet.com", SPECIALIST_PUBLICATION, 58, editorial_control=True,
       preferred_topics=[TRAVEL]),
    # --- social / low-signal -----------------------------------------------------------
    _p("pinterest.com", FORUM, 18, community_source=True, user_generated=True),
    _p("facebook.com", FORUM, 15, community_source=True, user_generated=True),
    _p("instagram.com", FORUM, 15, community_source=True, user_generated=True),
    _p("tiktok.com", FORUM, 14, community_source=True, user_generated=True),
    _p("twitter.com", FORUM, 20, community_source=True, user_generated=True,
       preferred_topics=[NEWS], notes="primary statements only when from the subject"),
    _p("x.com", FORUM, 20, community_source=True, user_generated=True,
       preferred_topics=[NEWS]),
    _p("youtube.com", FORUM, 26, community_source=True, user_generated=True,
       notes="creator channels vary wildly — official channels only when verified"),
    _p("answers.microsoft.com", FORUM, 40, community_source=True, user_generated=True,
       preferred_topics=[SOFTWARE],
       notes="mix of MVPs/support agents/users — answer flair varies"),
    _p("community.adobe.com", FORUM, 42, community_source=True, user_generated=True,
       preferred_topics=[SOFTWARE]),
]

# Topic keyword classification ---------------------------------------------------
_TOPIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (CYBERSECURITY, re.compile(
        r"\b(?:vulnerabilit|exploit|cve-|malware|ransomware|phishing|breach|"
        r"security\s+(?:patch|advisory|flaw|issue)|xss|injection\s+attack|0day|zero[- ]day)\b", re.I)),
    (PROGRAMMING, re.compile(
        r"\b(?:python|javascript|typescript|rust|golang|java|kotlin|c\+\+|c#|ruby|php|swift|"
        r"compile|function|variable|class|method|api|sdk|regex|async|await|thread|"
        r"exception|stack\s*trace|git\s|github|npm|pip|cargo|debug|refactor|"
        r"framework|library|package|module|runtime|interpreter|ide|vscode)\b", re.I)),
    (SOFTWARE, re.compile(
        r"\b(?:windows|linux|macos|ubuntu|debian|driver|firmware|install|uninstall|"
        r"software|app\b|application|program|version|release|update|upgrade|"
        r"cuda|nvidia|gpu|cpu|llama\.?cpp|comfyui|docker|kubernetes|database|"
        r"sql|server|cloud|aws|azure|operating\s+system|os\s+\d|"
        r"excel|word|photoshop|blender|unity|unreal)\b", re.I)),
    (MEDICAL, re.compile(
        r"\b(?:disease|diagnos|symptom|treatment|medication|drug\s|dosage|prescription|"
        r"cancer|diabetes|surgery|clinical|therapy|patient|vaccine|infection|"
        r"side\s+effects?|contraindicat|fda\s+approved|medical)\b", re.I)),
    (HEALTH, re.compile(
        r"\b(?:health|diet|nutrition|vitamin|exercise|fitness|weight\s+loss|"
        r"sleep|mental\s+health|anxiety|depression|blood\s+pressure|cholesterol|"
        r"calorie|protein\s+intake|wellness)\b", re.I)),
    (LEGAL, re.compile(
        r"\b(?:law|legal|statute|regulation|court|lawsuit|sue|liability|contract|"
        r"copyright|trademark|patent|gdpr|ccpa|compliance|subpoena|attorney|"
        r"constitution|amendment|ruling|verdict|settlement|terms\s+of\s+service|"
        r"privacy\s+policy|license\s+agreement|illegal|lawful)\b", re.I)),
    (SCIENCE, re.compile(
        r"\b(?:physics|chemistry|biology|quantum|molecule|atom|gene|dna|rna|"
        r"evolution|climate|geology|astronom|space|planet|research\s+(?:paper|study)|"
        r"peer[- ]review|experiment|hypothesis|theory|scientific)\b", re.I)),
    (FINANCE, re.compile(
        r"\b(?:stock|share|invest|portfolio|crypto|bitcoin|etf|bond|dividend|"
        r"interest\s+rate|mortgage|loan|credit\s+score|bank|trading|401k|ira|"
        r"tax|irs|deduction|inflation|price\s+of)\b", re.I)),
    (ECONOMICS, re.compile(
        r"\b(?:economy|economic|gdp|recession|unemployment|federal\s+reserve|"
        r"monetary\s+policy|fiscal|trade\s+deficit|tariff)\b", re.I)),
    (NEWS, re.compile(
        r"\b(?:news|breaking|today|headline|announced|just\s+released|"
        r"happened\s+(?:today|this)|latest\s+news|current\s+events?)\b", re.I)),
    (POLITICS, re.compile(
        r"\b(?:election|president|congress|senate|vote|voting|policy|"
        r"government\s+(?:shutdown|policy)|minister|parliament|democrat|republican|"
        r"bill\s+(?:passed|proposed)|legislation|executive\s+order)\b", re.I)),
    (PRODUCT_SPECS, re.compile(
        r"\b(?:specs?|specifications?|how\s+much\s+(?:ram|storage|memory|power)|"
        r"dimensions|battery\s+life|wattage|capacity|supports?\s+(?:up\s+to|a\s+max)|"
        r"model\s+number|datasheet|manual\b|official\s+spec)\b", re.I)),
    (CONSUMER_REVIEW, re.compile(
        r"\b(?:review|best\s+(?:phone|laptop|tv|headphone|camera|tablet|monitor|"
        r"keyboard|mouse|chair|mattress|vacuum|blender|air\s+fryer)|"
        r"worth\s+(?:buying|it)|should\s+i\s+buy|which\s+.*\s+should\s+i)\b", re.I)),
    (COOKING, re.compile(
        r"\b(?:recipe|cook|bake|grill|smoke[dr]?|sear|roast|braise|marinade|"
        r"ingredient|oven|skillet|sauce|gravy|seasoning|rub\b|gumbo|jambalaya|"
        r"ribeye|brisket|sourdough|dough|frying|saute|simmer|broil|"
        r"tablespoon|teaspoon|cup\s+of|fahrenheit|celsius\s+degrees)\b", re.I)),
    (HOME, re.compile(
        r"\b(?:plumbing|faucet|leak|drywall|hvac|furnace|thermostat|"
        r"electrical\s+(?:outlet|wiring)|circuit\s+breaker|roof|gutter|"
        r"lawn|garden|pest|mold|paint\s+a\s+room|insulation)\b", re.I)),
    (AUTOMOTIVE, re.compile(
        r"\b(?:car|truck|vehicle|engine|transmission|brake|tire|oil\s+change|"
        r"mpg|horsepower|torque|ev\s+charging|hybrid|alternator|spark\s+plug|"
        r"check\s+engine)\b", re.I)),
    (TRAVEL, re.compile(
        r"\b(?:flight|hotel|visa|passport|itinerary|vacation|trip\s+to|"
        r"travel\s+to|airport|luggage|tourist|destination)\b", re.I)),
    (HISTORY, re.compile(
        r"\b(?:history|historical|ancient|medieval|world\s+war|civil\s+war|"
        r"century|dynasty|empire|revolution\s+of|founded\s+in)\b", re.I)),
    (ACADEMIC, re.compile(
        r"\b(?:homework|essay|thesis|dissertation|coursework|citation|"
        r"university|college\s+(?:course|class)|study\s+guide|exam)\b", re.I)),
]

_GITHUB_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.I)
_GITHUB_NOISE = {"topics", "explore", "trending", "collections", "features",
                 "marketplace", "pricing", "login", "signup", "settings",
                 "search", "orgs", "users", "new", "notifications", "sponsors"}


class TopicClassifier:
    """Classify a research query into topic categories (Part 5)."""

    def classify(self, query: str) -> list[str]:
        text = str(query or "")
        topics: list[str] = []
        for topic, pattern in _TOPIC_PATTERNS:
            if pattern.search(text):
                topics.append(topic)
        if not topics:
            topics.append(GENERAL)
        # SOFTWARE and PROGRAMMING frequently co-fire; keep both but put the
        # more specific one first.
        if PROGRAMMING in topics and SOFTWARE in topics:
            topics.remove(SOFTWARE)
            topics.insert(1 if topics and topics[0] == PROGRAMMING else 0, SOFTWARE)
        return topics

    def high_stakes(self, topics: list[str]) -> bool:
        return bool(set(topics) & HIGH_STAKES_TOPICS)


class SourceTrustRegistry:
    """Prior knowledge about source classes/domains (Part 1)."""

    schema_version = 1

    def __init__(self, extra_profiles: list[SourceTrustProfile] | None = None) -> None:
        self._profiles: dict[str, SourceTrustProfile] = {}
        self._dynamic: dict[str, SourceTrustProfile] = {}
        for profile in _SEED:
            self.register(profile)
        for profile in extra_profiles or []:
            self.register(profile)

    def register(self, profile: SourceTrustProfile) -> None:
        domain = _norm_host(profile.domain)
        if domain:
            profile.domain = domain
            self._profiles[domain] = profile

    def register_dynamic(self, domain: str, profile: SourceTrustProfile) -> None:
        """Register a resolved-at-runtime official domain (e.g. a PyPI
        package's documentation site). Dynamic profiles are scoped to the
        packages they were resolved for via `official_for`."""
        host = _norm_host(domain)
        if not host:
            return
        profile.domain = host
        self._dynamic[host] = profile

    @staticmethod
    def _hosts(url_or_domain: str) -> list[str]:
        host = _norm_host(url_or_domain)
        if not host:
            return []
        parts = host.split(".")
        hosts = [host]
        for i in range(1, len(parts) - 1):
            hosts.append(".".join(parts[i:]))
        return hosts

    def profile_for(self, url_or_domain: str) -> SourceTrustProfile:
        """Most specific profile for a URL/domain; sensible default otherwise."""
        for host in self._hosts(url_or_domain):
            if host in self._dynamic:
                return self._dynamic[host]
            if host in self._profiles:
                return self._profiles[host]
        # TLD heuristics for unlisted domains.
        host = _norm_host(url_or_domain)
        if host.endswith(".gov") or host.endswith(".gov.uk") or host.endswith(".gc.ca"):
            return _p(host, GOVERNMENT, 78, editorial_control=True,
                      preferred_topics=[LEGAL, NEWS, HEALTH, SCIENCE])
        if host.endswith(".edu") or host.endswith(".ac.uk"):
            return _p(host, ACADEMIC_INSTITUTION, 66, editorial_control=True,
                      preferred_topics=[ACADEMIC, SCIENCE])
        if host.endswith(".mil"):
            return _p(host, GOVERNMENT, 78, editorial_control=True,
                      preferred_topics=[LEGAL, NEWS])
        return _p(host, UNKNOWN, 30)

    # ------------------------------------------------------------------
    # GitHub: official repo vs community repo (Part 24)
    # ------------------------------------------------------------------
    @staticmethod
    def github_repo(url: str) -> tuple[str, str] | None:
        m = _GITHUB_RE.search(str(url or ""))
        if not m:
            return None
        owner, repo = m.group(1).lower(), m.group(2).lower().removesuffix(".git")
        if owner in _GITHUB_NOISE or repo in _GITHUB_NOISE:
            return None
        return owner, repo

    def github_repo_class(
        self,
        url: str,
        expected: list[str] | None = None,
        query: str = "",
    ) -> str:
        """Classify a github.com URL. `expected` — 'owner/repo' or 'owner'
        hints from package metadata/tech mapping; query terms also count —
        'llama.cpp' makes ggml-org/llama.cpp look official."""
        parsed = self.github_repo(url)
        if not parsed:
            return "community"
        owner, repo = parsed
        full = f"{owner}/{repo}"
        hints = {str(e).lower() for e in (expected or [])}
        if full in hints or owner in hints:
            return "official_repo"
        # Name match: repo named exactly like a product token in the query.
        q = str(query or "").lower()
        tokens = {t.strip(".-") for t in re.findall(r"[a-z][\w.-]{2,}", q)}
        normal = {t.replace(".", "").replace("-", "") for t in tokens}
        repo_norm = repo.replace(".", "").replace("-", "")
        if repo_norm and repo_norm in normal and len(repo_norm) >= 3:
            # e.g. query "llama.cpp release" + repo ggml-org/llama.cpp
            return "official_repo"
        if repo_norm and any(repo_norm == n or repo_norm in n for n in normal if len(n) > 5):
            return "project_repo"
        return "community"

    # ------------------------------------------------------------------
    # Topic-aware authority (Part 4)
    # ------------------------------------------------------------------
    def topic_authority(
        self, url_or_domain: str, topics: list[str]
    ) -> tuple[float, SourceTrustProfile, list[str]]:
        """(authority, profile, reasons) — combines base_authority with
        topic fit. A source authoritative *for this subject* gets its full
        base; off-topic authority decays hard."""
        profile = self.profile_for(url_or_domain)
        base = profile.base_authority
        reasons: list[str] = []
        topic_set = set(topics) or {GENERAL}
        official_hit = topic_set & set(profile.official_for)
        preferred_hit = topic_set & set(profile.preferred_topics)
        discouraged_hit = topic_set & set(profile.discouraged_topics)
        if official_hit:
            authority = base + 8.0
            reasons.append(f"official for {sorted(official_hit)[0]}")
        elif preferred_hit:
            authority = base + 4.0
            reasons.append(f"specialist for {sorted(preferred_hit)[0]}")
        elif discouraged_hit:
            authority = base - 25.0
            reasons.append(f"discouraged for {sorted(discouraged_hit)[0]}")
        elif profile.category in {
            OFFICIAL_DOCUMENTATION, PRIMARY_OFFICIAL, STANDARD_BODY,
            GOVERNMENT, PEER_REVIEWED_RESEARCH,
        }:
            # High-tier source off its home turf — reputation doesn't
            # transfer to unrelated topics (docs.python.org on gumbo).
            authority = base * 0.45 if GENERAL not in topic_set else base * 0.7
            reasons.append("high-tier source off-topic")
        elif profile.category in {SPECIALIST_PUBLICATION, MAJOR_PROFESSIONAL_SOURCE}:
            authority = base * 0.6
            reasons.append("specialist outside its specialty")
        elif topic_set == {GENERAL} or GENERAL in topic_set:
            authority = base * 0.9
        else:
            authority = base * 0.8
        if profile.preprint:
            authority -= 12.0
            reasons.append("preprint — not peer reviewed")
        return max(0.0, min(100.0, authority)), profile, reasons


def _norm_host(url_or_domain: str) -> str:
    text = str(url_or_domain or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0].split("?", 1)[0]
    return text.removeprefix("www.")
