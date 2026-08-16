/*
 * ===========================================================================
 *  Pricefx PriceList Calculation logic -- native equivalent of pfx/rules.py
 * ===========================================================================
 *
 *  WHY THIS EXISTS
 *  ---------------
 *  You asked to keep things native to Pricefx where possible. There are two
 *  places the price maths can live, and they are not equivalent:
 *
 *    (A) In the runner (pfx/rules.py). Prices are computed outside Pricefx and
 *        written onto the line items. Fastest to iterate, fully unit-tested,
 *        works against a CSV or any external feed. Downside: a user who edits
 *        a line item in the Pricefx UI and hits recalculate does NOT get your
 *        logic -- the number is just data.
 *
 *    (B) Here, as a PriceList Calculation logic. Pricefx owns the maths. The
 *        UI recalculate button works, results are reproducible inside the
 *        platform, and pricing analysts can see each element's contribution.
 *        Downside: iteration is slower and unit testing is awkward.
 *
 *  Recommended: (B) for the maths, with the runner doing sourcing, segmenting
 *  and orchestration. The runner then creates the list, attaches this logic and
 *  calls calculate. Both paths read the SAME rules JSON, so the two can never
 *  silently disagree -- see loadRules() below.
 *
 *  HOW TO INSTALL
 *  --------------
 *   1. Pricefx Studio (or Administration > Logics) > new PriceList Calculation
 *      logic named e.g. PriceListLogic.
 *   2. Create one element per section below, in this order. Each element's
 *      name must match, because later elements read earlier ones by name.
 *   3. Put the rules JSON in a Company Parameter table (default: PricingRules,
 *      key column `name` = the ruleset name, value column `value` = the JSON).
 *      `python3 -m pfx.cli export-rules` emits exactly that JSON from the YAML.
 *   4. Set price_list.calculation_logic in config.yaml to the logic's name.
 *
 *  Elements, in evaluation order:
 *      1  BaseCost        pick the starting number
 *      2  TierMarkup      band-based markup
 *      3  Uplifts         if/then adjustments
 *      4  Bounds          margin floor / caps
 *      5  ResultPrice     final rounding  <- the price list's output field
 *      6  AuditTrail      human-readable explanation (String)
 * ===========================================================================
 */

// ===========================================================================
// ELEMENT 1: BaseCost   (type: Money / BigDecimal)
// ===========================================================================
/*
def rules = api.getElement("Rules")           // see the Rules element below
def base  = rules.base

def value = null
def usedField = null
for (String f : ([base.field] + (base.fallback_field ?: []))) {
    def raw = productFieldValue(f)
    if (raw != null && raw.toString().trim() != "") {
        value = toBD(raw)
        usedField = f
        break
    }
}

if (value == null) {
    // on_missing: skip -> abort this line item cleanly with a visible reason.
    if (base.on_missing == "error") {
        api.throwException("No base price for SKU ${api.currentItem()?.sku}")
    }
    if (base.on_missing == "zero") { return 0.0G }
    api.addWarning("No base price for SKU ${api.currentItem()?.sku}")
    return null                                // null result => item is skipped
}

api.trace("BaseCost", "from ${usedField}", value)
return value
*/


// ===========================================================================
// ELEMENT 2: TierMarkup   (type: Money / BigDecimal)
// ===========================================================================
/*
def rules = api.getElement("Rules")
def base  = api.getElement("BaseCost")
if (base == null) return null

def cfg = rules.tiers
if (cfg == null) return base

// Band driver: "base", or any product field.
def driver = base
if (cfg.on && cfg.on != "base") {
    def raw = productFieldValue(cfg.on)
    if (raw == null || raw.toString().trim() == "") {
        api.addWarning("Tier driver ${cfg.on} missing; banding on base price instead")
    } else {
        driver = toBD(raw)
    }
}

// Bands are HALF-OPEN [min, max) so a value on a boundary matches exactly one.
def chosen = null
def bandName = "default"
for (band in (cfg.bands ?: [])) {
    if (band.min != null && driver < toBD(band.min)) continue
    if (band.max != null && driver >= toBD(band.max)) continue
    chosen   = toBD(band.value)
    bandName = band.name ?: "band"
    break
}
if (chosen == null) {
    if (cfg.default == null) return base
    chosen = toBD(cfg.default)
}

def out
switch (cfg.mode ?: "percent") {
    case "percent":    out = base * (1.0G + chosen / 100.0G); break
    case "multiplier": out = base * chosen;                   break
    default:           out = base + chosen                    // absolute
}
api.trace("TierMarkup", "${bandName} on driver ${driver}", out)
return out
*/


// ===========================================================================
// ELEMENT 3: Uplifts   (type: Money / BigDecimal)
// ===========================================================================
/*
def rules = api.getElement("Rules")
def value = api.getElement("TierMarkup")
if (value == null) return null

def cfg = rules.uplifts
if (cfg == null) return value

def firstMatchOnly = (cfg.mode == "first_match")

for (rule in (cfg.rules ?: [])) {
    if (!matches(rule.when)) continue
    def then = rule.then

    def operand = { spec ->
        // A leading '@' reads the value off the row instead of a literal.
        if (spec instanceof String && spec.startsWith("@")) {
            def raw = productFieldValue(spec.substring(1))
            return (raw == null || raw.toString().trim() == "") ? null : toBD(raw)
        }
        return toBD(spec)
    }

    if (then.containsKey("percent")) {
        def p = operand(then.percent); if (p == null) continue
        value = value * (1.0G + p / 100.0G)
    } else if (then.containsKey("multiplier")) {
        def m = operand(then.multiplier); if (m == null) continue
        value = value * m
    } else if (then.containsKey("absolute")) {
        def a = operand(then.absolute); if (a == null) continue
        value = value + a
    } else {
        def s = operand(then.set); if (s == null) continue
        value = s
    }
    api.trace("Uplifts", rule.name as String, value)
    if (firstMatchOnly) break
}
return value
*/


// ===========================================================================
// ELEMENT 4: Bounds   (type: Money / BigDecimal)
// ===========================================================================
/*
def rules = api.getElement("Rules")
def value = api.getElement("Uplifts")
if (value == null) return null

def cfg = rules.bounds
if (cfg == null) return value

// Margin floor first: it is a hard commercial constraint and an absolute floor
// set below cost must not be able to override it.
if (cfg.min_margin_percent != null && cfg.cost_field) {
    def rawCost = productFieldValue(cfg.cost_field)
    if (rawCost != null && rawCost.toString().trim() != "") {
        def cost = toBD(rawCost)
        def pct  = toBD(cfg.min_margin_percent)
        if (pct >= 100.0G) api.throwException("bounds.min_margin_percent must be below 100")
        def floorPrice = cost / (1.0G - pct / 100.0G)
        if (value < floorPrice) {
            api.addMessage("Margin floor applied: ${value} -> ${floorPrice}")
            value = floorPrice
        }
    }
}

if (cfg.floor   != null && value < toBD(cfg.floor))   value = toBD(cfg.floor)
if (cfg.ceiling != null && value > toBD(cfg.ceiling)) value = toBD(cfg.ceiling)

if (cfg.floor_field) {
    def raw = productFieldValue(cfg.floor_field)
    if (raw != null && raw.toString().trim() != "" && value < toBD(raw)) value = toBD(raw)
}
if (cfg.ceiling_field) {
    def raw = productFieldValue(cfg.ceiling_field)
    if (raw != null && raw.toString().trim() != "" && value > toBD(raw)) value = toBD(raw)
}
return value
*/


// ===========================================================================
// ELEMENT 5: ResultPrice   (type: Money)  <- the price list output field
// ===========================================================================
/*
def rules = api.getElement("Rules")
def value = api.getElement("Bounds")
if (value == null) return null

def cfg = rules.rounding
if (cfg == null) return value.setScale(2, java.math.RoundingMode.HALF_UP)

def rule = null
for (ov in (cfg.overrides ?: [])) {
    if (matches(ov.when)) { rule = ov.rule; break }
}
if (rule == null) rule = cfg.default
if (rule == null) return value.setScale(2, java.math.RoundingMode.HALF_UP)

return applyRounding(value, rule)
*/


// ===========================================================================
// ELEMENT 6: AuditTrail   (type: String)
// ===========================================================================
/*
// Mirrors the runner's audit trail so a number can be explained in the UI.
def parts = []
["BaseCost", "TierMarkup", "Uplifts", "Bounds", "ResultPrice"].each { name ->
    def v = api.getElement(name)
    if (v != null) parts << "${name}=${v}"
}
return parts.join(" | ")
*/


// ===========================================================================
// ELEMENT 0: Rules   (type: Object, hidden)  -- put this FIRST
// ===========================================================================
/*
// Single source of truth. The same JSON the Python runner reads, so the two
// implementations cannot drift apart. Regenerate with:
//     python3 -m pfx.cli export-rules --rules rules.yaml > rules.json
// and paste it into the PricingRules company parameter.
def name = api.getParameter("rulesetName")?.value ?: "default"
def row  = api.findLookupTableValues("PricingRules", Filter.equal("name", name))?.find { true }
if (row == null) api.throwException("No PricingRules entry named '${name}'")
return new groovy.json.JsonSlurper().parseText(row.value as String)
*/


// ===========================================================================
// SHARED HELPERS  -- put these in a Groovy library logic and include it, or
// paste into each element that needs them.
// ===========================================================================

/**
 * Coerce a source value into BigDecimal.
 * ERP feeds hand back numbers as strings, sometimes with thousands separators.
 * new BigDecimal("1,234.50") throws; this does not.
 */
static java.math.BigDecimal toBD(Object v) {
    if (v == null) return null
    if (v instanceof java.math.BigDecimal) return v
    if (v instanceof Number) return new java.math.BigDecimal(v.toString())
    String s = v.toString().trim().replace(",", "").replace(" ", "")
    if (s == "" || s.equalsIgnoreCase("null") || s == "-") return null
    return new java.math.BigDecimal(s)
}

/**
 * Read a field off the current product / price list item.
 * Supports dotted access, since custom fields nest differently per tenant.
 */
def productFieldValue(String name) {
    def item = api.currentItem()
    if (item != null && item[name] != null) return item[name]
    def p = api.product(name)
    if (p != null) return p
    if (name.contains(".")) {
        def cur = api.currentItem()
        for (String part : name.split("\\.")) {
            if (cur == null) return null
            cur = cur[part]
        }
        return cur
    }
    return null
}

/** Case-insensitive, whitespace-tolerant comparison key. */
static Object norm(Object v) {
    return (v instanceof String) ? v.trim().toLowerCase() : v
}

/**
 * Evaluate a condition tree. Same syntax as the YAML `when:` / `filter:` blocks:
 *   [field: "value"] / [field: [gt: 100]] / [all: [...]] / [any: [...]] / [not: [...]]
 */
boolean matches(Object cond) {
    if (cond == null || cond == true) return true
    if (cond == false) return false
    if (!(cond instanceof Map)) api.throwException("condition must be a map, got ${cond?.getClass()}")

    for (entry in (cond as Map)) {
        String key = entry.key as String
        def spec = entry.value

        if (key == "all") { if (!(spec as List).every { matches(it) }) return false; continue }
        if (key == "any") { if (!(spec as List).any   { matches(it) }) return false; continue }
        if (key == "not") { if (matches(spec))                          return false; continue }

        def actual = productFieldValue(key)
        if (spec instanceof Map) {
            for (op in (spec as Map)) {
                if (!applyOperator(op.key as String, actual, op.value)) return false
            }
        } else {
            if (norm(actual) != norm(spec)) return false
        }
    }
    return true
}

boolean applyOperator(String op, Object actual, Object expected) {
    def numeric = { java.util.function.BiPredicate<java.math.BigDecimal, java.math.BigDecimal> f ->
        def a = toBD(actual), e = toBD(expected)
        return (a != null && e != null) && f.test(a, e)
    }
    switch (op) {
        case "eq":         return norm(actual) == norm(expected)
        case "ne":         return norm(actual) != norm(expected)
        case "in":         return (expected as List).collect { norm(it) }.contains(norm(actual))
        case "not_in":     return !(expected as List).collect { norm(it) }.contains(norm(actual))
        case "gt":         return numeric({ a, e -> a >  e })
        case "gte":        return numeric({ a, e -> a >= e })
        case "lt":         return numeric({ a, e -> a <  e })
        case "lte":        return numeric({ a, e -> a <= e })
        case "contains":   return actual != null && actual.toString().toLowerCase().contains(expected.toString().toLowerCase())
        case "startswith": return actual != null && actual.toString().toLowerCase().startsWith(expected.toString().toLowerCase())
        case "endswith":   return actual != null && actual.toString().toLowerCase().endsWith(expected.toString().toLowerCase())
        case "regex":      return actual != null && (actual.toString() =~ expected.toString()).find()
        case "exists":     return ((actual != null && actual.toString() != "") == (expected as boolean))
        case "is_null":    return ((actual == null || actual.toString() == "") == (expected as boolean))
        default:
            api.throwException("Unknown operator '${op}'")
            return false
    }
}

/** Round to a multiple of `increment`, without scientific notation. */
static java.math.BigDecimal quantizeTo(java.math.BigDecimal value,
                                       java.math.BigDecimal increment,
                                       java.math.RoundingMode mode) {
    if (increment <= 0.0G) throw new IllegalArgumentException("rounding increment must be > 0")
    def steps = value.divide(increment, 0, mode)
    def scale = Math.max(increment.scale(), 0)
    return steps.multiply(increment).setScale(scale, java.math.RoundingMode.HALF_UP)
}

/** Apply one rounding rule. Mirrors pfx/rules.py apply_rounding() exactly. */
java.math.BigDecimal applyRounding(java.math.BigDecimal value, Map rule) {
    String method = (rule.method ?: "nearest").toString().toLowerCase()
    def RM = java.math.RoundingMode

    if (method == "decimals") {
        return value.setScale((rule.decimals ?: 2) as int, RM.HALF_UP)
    }
    if (method in ["nearest", "up", "down"]) {
        def inc = toBD(rule.increment ?: "0.01")
        def mode = (method == "nearest") ? RM.HALF_UP : (method == "up" ? RM.CEILING : RM.FLOOR)
        return quantizeTo(value, inc, mode)
    }
    if (method == "charm") {
        def inc = toBD(rule.increment ?: "1")
        def end = toBD(rule.ending ?: "0.99")
        if (end >= inc) api.throwException("charm rounding: ending ${end} must be smaller than increment ${inc}")
        def floorVal  = quantizeTo(value, inc, RM.FLOOR)
        def candidate = floorVal + end
        if ((rule.direction ?: "up").toString().toLowerCase() == "down" && candidate > value) {
            candidate = floorVal - inc + end
        }
        if (candidate < 0.0G) candidate = end
        return candidate
    }
    if (method == "endings") {
        def endings = (rule.endings ?: []).collect { toBD(it) }
        if (endings.isEmpty()) api.throwException("rounding method 'endings' needs a non-empty endings list")
        def unit  = toBD(rule.unit ?: "1")
        def whole = quantizeTo(value, unit, RM.FLOOR)
        def candidates = endings.collect { whole + it } + endings.collect { whole + unit + it }
        if ((rule.direction ?: "nearest").toString().toLowerCase() == "down") {
            def below = candidates.findAll { it <= value }
            return below ? below.max() : candidates.min()
        }
        return candidates.min { (it - value).abs() }
    }
    api.throwException("Unknown rounding method '${method}'")
    return value
}
