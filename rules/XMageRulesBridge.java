package dev.mtgbot.xmage;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.util.*;

import mage.cards.Card;
import mage.cards.CardImpl;
import mage.cards.repository.CardInfo;
import mage.cards.repository.CardRepository;
import mage.cards.repository.CardScanner;
import mage.constants.*;
import mage.game.Game;
import mage.game.GameImpl;

import mage.game.permanent.Permanent;
import mage.players.Player;
import mage.abilities.Ability;
import mage.abilities.effects.Effect;
import mage.target.Target;

/**
 * XMage Rules Bridge
 * 
 * A JSON-RPC style interface to XMage's rules engine.
 * Accepts commands via stdin, returns results via stdout.
 * 
 * Commands:
 * - {"cmd": "lookup", "card": "Lightning Bolt"} - Get card info
 * - {"cmd": "resolve", "card": "Lightning Bolt", "target": {...}, "state": {...}} - Resolve ability
 * - {"cmd": "validate", "action": "cast", "card": "...", "state": {...}} - Check if action is legal
 * - {"cmd": "triggers", "event": "enters", "state": {...}} - Get triggered abilities
 * - {"cmd": "state_based", "state": {...}} - Run state-based actions
 * - {"cmd": "combat", "attackers": [...], "blockers": {...}, "state": {...}} - Resolve combat
 */
public class XMageRulesBridge {
    
    private final Gson gson = new Gson();
    private final CardRepository cardRepo;
    
    public XMageRulesBridge() {
        // Force card scanning to populate the database
        System.err.println("Initializing card database...");
        try {
            CardScanner.scan();
            System.err.println("Card scan complete.");
        } catch (Exception e) {
            System.err.println("Card scan failed: " + e.getMessage());
            e.printStackTrace(System.err);
        }
        
        // Initialize card repository
        this.cardRepo = CardRepository.instance;
    }
    
    public static void main(String[] args) {
        XMageRulesBridge bridge = new XMageRulesBridge();
        bridge.run();
    }
    
    public void run() {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
             PrintWriter writer = new PrintWriter(System.out, true)) {
            
            // Signal ready
            writer.println(gson.toJson(Map.of("status", "ready", "version", "1.0.0")));
            
            String line;
            while ((line = reader.readLine()) != null) {
                JsonObject response;
                try {
                    JsonObject request = gson.fromJson(line, JsonObject.class);
                    response = handleRequest(request);
                } catch (Exception e) {
                    response = errorResponse("parse_error", e.getMessage());
                }
                writer.println(gson.toJson(response));
            }
        } catch (Exception e) {
            System.err.println("Fatal error: " + e.getMessage());
            e.printStackTrace();
        }
    }
    
    private JsonObject handleRequest(JsonObject request) {
        String cmd = request.has("cmd") ? request.get("cmd").getAsString() : "";
        
        try {
            switch (cmd) {
                case "lookup":
                    return handleLookup(request);
                case "resolve":
                    return handleResolve(request);
                case "validate":
                    return handleValidate(request);
                case "triggers":
                    return handleTriggers(request);
                case "state_based":
                    return handleStateBasedActions(request);
                case "combat":
                    return handleCombat(request);
                case "keywords":
                    return handleKeywords(request);
                case "ping":
                    return successResponse(Map.of("pong", true));
                default:
                    return errorResponse("unknown_command", "Unknown command: " + cmd);
            }
        } catch (Exception e) {
            return errorResponse("execution_error", e.getMessage());
        }
    }
    
    // =========================================================================
    // Command Handlers
    // =========================================================================
    
    /**
     * Look up a card by name, return its properties
     */
    private JsonObject handleLookup(JsonObject request) {
        String cardName = request.get("card").getAsString();
        
        CardInfo cardInfo = cardRepo.findCard(cardName);
        if (cardInfo == null) {
            // Try partial match
            List<CardInfo> matches = cardRepo.findCards(cardName);
            if (matches.isEmpty()) {
                return errorResponse("card_not_found", "No card found: " + cardName);
            }
            cardInfo = matches.get(0);
        }
        
        Card card = cardInfo.createMockCard();
        
        Map<String, Object> cardData = new LinkedHashMap<>();
        cardData.put("name", card.getName());
        cardData.put("manaCost", card.getManaCostSymbols().toString());
        cardData.put("cmc", card.getManaValue());
        cardData.put("types", getTypes(card));
        cardData.put("subtypes", getSubtypes(card));
        cardData.put("supertypes", getSupertypes(card));
        cardData.put("text", card.getRules().toString());
        cardData.put("power", card.getPower() != null ? card.getPower().toString() : null);
        cardData.put("toughness", card.getToughness() != null ? card.getToughness().toString() : null);
        cardData.put("colors", getColors(card));
        cardData.put("keywords", getKeywords(card));
        cardData.put("abilities", getAbilities(card));
        
        return successResponse(cardData);
    }
    
    /**
     * Resolve a spell/ability given game state
     */
    private JsonObject handleResolve(JsonObject request) {
        String cardName = request.get("card").getAsString();
        JsonObject stateJson = request.has("state") ? request.getAsJsonObject("state") : new JsonObject();
        JsonObject targetJson = request.has("target") ? request.getAsJsonObject("target") : null;
        
        // Create a test game from the state
        TestGameState gameState = parseGameState(stateJson);
        
        // Find the card
        CardInfo cardInfo = cardRepo.findCard(cardName);
        if (cardInfo == null) {
            return errorResponse("card_not_found", "No card found: " + cardName);
        }
        
        // For now, return the expected effects based on card abilities
        // Full resolution would require setting up the complete XMage game engine
        Card card = cardInfo.createMockCard();
        
        List<Map<String, Object>> effects = new ArrayList<>();
        for (Ability ability : card.getAbilities()) {
            for (Effect effect : ability.getEffects()) {
                Map<String, Object> effectData = new LinkedHashMap<>();
                effectData.put("type", effect.getClass().getSimpleName());
                effectData.put("text", effect.getText(ability.getModes().getMode()));
                effectData.put("outcome", effect.getOutcome().toString());
                effects.add(effectData);
            }
        }
        
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("card", cardName);
        result.put("resolved", true);
        result.put("effects", effects);
        result.put("newState", gameState.toJson());
        
        return successResponse(result);
    }
    
    /**
     * Validate if an action is legal
     */
    private JsonObject handleValidate(JsonObject request) {
        String action = request.get("action").getAsString();
        String cardName = request.has("card") ? request.get("card").getAsString() : null;
        JsonObject stateJson = request.has("state") ? request.getAsJsonObject("state") : new JsonObject();
        
        TestGameState gameState = parseGameState(stateJson);
        
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("action", action);
        result.put("card", cardName);
        
        switch (action) {
            case "cast":
                result.put("legal", validateCast(cardName, gameState));
                break;
            case "activate":
                result.put("legal", validateActivate(cardName, gameState));
                break;
            case "attack":
                result.put("legal", validateAttack(cardName, gameState));
                break;
            case "block":
                result.put("legal", validateBlock(cardName, request, gameState));
                break;
            default:
                result.put("legal", false);
                result.put("reason", "Unknown action type");
        }
        
        return successResponse(result);
    }
    
    /**
     * Get triggered abilities for an event
     */
    private JsonObject handleTriggers(JsonObject request) {
        String event = request.get("event").getAsString();
        JsonObject stateJson = request.has("state") ? request.getAsJsonObject("state") : new JsonObject();
        String sourceCard = request.has("source") ? request.get("source").getAsString() : null;
        
        TestGameState gameState = parseGameState(stateJson);
        
        List<Map<String, Object>> triggers = new ArrayList<>();
        
        // Check all permanents for triggered abilities
        for (TestPermanent perm : gameState.battlefield) {
            CardInfo cardInfo = cardRepo.findCard(perm.name);
            if (cardInfo == null) continue;
            
            Card card = cardInfo.createMockCard();
            for (Ability ability : card.getAbilities()) {
                // Check if this is a triggered ability that matches the event
                String abilityType = ability.getClass().getSimpleName();
                String abilityText = ability.getRule().toLowerCase();
                
                boolean triggers_on_event = false;
                
                switch (event) {
                    case "enters":
                        triggers_on_event = abilityText.contains("enters") || 
                                           abilityText.contains("when") && abilityText.contains("enter");
                        break;
                    case "dies":
                        triggers_on_event = abilityText.contains("dies") ||
                                           abilityText.contains("when") && abilityText.contains("die");
                        break;
                    case "attacks":
                        triggers_on_event = abilityText.contains("attacks") ||
                                           abilityText.contains("whenever") && abilityText.contains("attack");
                        break;
                    case "damage":
                        triggers_on_event = abilityText.contains("deals damage") ||
                                           abilityText.contains("dealt damage");
                        break;
                    case "upkeep":
                        triggers_on_event = abilityText.contains("at the beginning of") && 
                                           abilityText.contains("upkeep");
                        break;
                    case "end_step":
                        triggers_on_event = abilityText.contains("at the beginning of") && 
                                           (abilityText.contains("end step") || abilityText.contains("end of turn"));
                        break;
                }
                
                if (triggers_on_event) {
                    Map<String, Object> triggerData = new LinkedHashMap<>();
                    triggerData.put("source", perm.name);
                    triggerData.put("controller", perm.controller);
                    triggerData.put("ability", ability.getRule());
                    triggerData.put("mandatory", !abilityText.contains("you may"));
                    triggers.add(triggerData);
                }
            }
        }
        
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("event", event);
        result.put("triggers", triggers);
        
        return successResponse(result);
    }
    
    /**
     * Run state-based actions and return changes
     */
    private JsonObject handleStateBasedActions(JsonObject request) {
        JsonObject stateJson = request.has("state") ? request.getAsJsonObject("state") : new JsonObject();
        TestGameState gameState = parseGameState(stateJson);
        
        List<Map<String, Object>> actions = new ArrayList<>();
        
        // Check creature death from damage/toughness
        Iterator<TestPermanent> it = gameState.battlefield.iterator();
        while (it.hasNext()) {
            TestPermanent perm = it.next();
            if (perm.isCreature) {
                int effectiveToughness = perm.toughness + perm.toughnessModifier + perm.plusCounters - perm.minusCounters;
                if (effectiveToughness <= 0 || perm.damageMarked >= effectiveToughness) {
                    Map<String, Object> action = new LinkedHashMap<>();
                    action.put("type", "creature_dies");
                    action.put("permanent", perm.name);
                    action.put("reason", effectiveToughness <= 0 ? "zero_toughness" : "lethal_damage");
                    actions.add(action);
                    
                    // Move to graveyard
                    gameState.graveyards.computeIfAbsent(perm.controller, k -> new ArrayList<>()).add(perm.name);
                    it.remove();
                }
            }
        }
        
        // Check player death
        for (Map.Entry<String, Integer> entry : gameState.playerLife.entrySet()) {
            if (entry.getValue() <= 0) {
                Map<String, Object> action = new LinkedHashMap<>();
                action.put("type", "player_loses");
                action.put("player", entry.getKey());
                action.put("reason", "life_zero");
                actions.add(action);
            }
        }
        
        // Check poison counters
        for (Map.Entry<String, Integer> entry : gameState.poisonCounters.entrySet()) {
            if (entry.getValue() >= 10) {
                Map<String, Object> action = new LinkedHashMap<>();
                action.put("type", "player_loses");
                action.put("player", entry.getKey());
                action.put("reason", "poison");
                actions.add(action);
            }
        }
        
        // Legend rule
        Map<String, List<TestPermanent>> legendsByName = new HashMap<>();
        for (TestPermanent perm : gameState.battlefield) {
            if (perm.isLegendary) {
                legendsByName.computeIfAbsent(perm.name + ":" + perm.controller, k -> new ArrayList<>()).add(perm);
            }
        }
        for (Map.Entry<String, List<TestPermanent>> entry : legendsByName.entrySet()) {
            if (entry.getValue().size() > 1) {
                Map<String, Object> action = new LinkedHashMap<>();
                action.put("type", "legend_rule");
                action.put("legend", entry.getKey().split(":")[0]);
                action.put("controller", entry.getKey().split(":")[1]);
                action.put("choice_required", true);
                actions.add(action);
            }
        }
        
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("actions", actions);
        result.put("newState", gameState.toJson());
        
        return successResponse(result);
    }
    
    /**
     * Resolve combat damage
     */
    private JsonObject handleCombat(JsonObject request) {
        JsonArray attackersJson = request.has("attackers") ? request.getAsJsonArray("attackers") : new JsonArray();
        JsonObject blockersJson = request.has("blockers") ? request.getAsJsonObject("blockers") : new JsonObject();
        JsonObject stateJson = request.has("state") ? request.getAsJsonObject("state") : new JsonObject();
        String damageStep = request.has("step") ? request.get("step").getAsString() : "normal";
        
        TestGameState gameState = parseGameState(stateJson);
        
        List<Map<String, Object>> damageEvents = new ArrayList<>();
        Map<String, Integer> lifelinkGains = new HashMap<>();
        
        // Process each attacker
        for (JsonElement attackerEl : attackersJson) {
            String attackerName = attackerEl.getAsString();
            TestPermanent attacker = gameState.findPermanent(attackerName);
            if (attacker == null) continue;
            
            // Get keywords
            Set<String> attackerKeywords = getKeywordsFromState(attacker, gameState);
            
            // Check if deals damage this step
            boolean hasFirstStrike = attackerKeywords.contains("First strike") || attackerKeywords.contains("Double strike");
            boolean hasDoubleStrike = attackerKeywords.contains("Double strike");
            
            if (damageStep.equals("first_strike") && !hasFirstStrike) continue;
            if (damageStep.equals("normal") && hasFirstStrike && !hasDoubleStrike) continue;
            
            int attackerPower = attacker.power + attacker.powerModifier + attacker.plusCounters - attacker.minusCounters;
            if (attackerPower <= 0) continue;
            
            // Get blockers for this attacker
            List<String> blockerNames = new ArrayList<>();
            if (blockersJson.has(attackerName)) {
                for (JsonElement b : blockersJson.getAsJsonArray(attackerName)) {
                    blockerNames.add(b.getAsString());
                }
            }
            
            if (blockerNames.isEmpty()) {
                // Unblocked - damage to defending player
                String defender = attacker.controller.equals("playerA") ? "playerB" : "playerA";
                
                Map<String, Object> dmgEvent = new LinkedHashMap<>();
                dmgEvent.put("source", attackerName);
                dmgEvent.put("target", defender);
                dmgEvent.put("amount", attackerPower);
                dmgEvent.put("type", "combat_damage");
                damageEvents.add(dmgEvent);
                
                gameState.playerLife.merge(defender, -attackerPower, Integer::sum);
                
                if (attackerKeywords.contains("Lifelink")) {
                    lifelinkGains.merge(attacker.controller, attackerPower, Integer::sum);
                }
            } else {
                // Blocked - assign damage to blockers
                int remainingDamage = attackerPower;
                boolean hasDeathtouch = attackerKeywords.contains("Deathtouch");
                boolean hasTrample = attackerKeywords.contains("Trample");
                
                for (String blockerName : blockerNames) {
                    if (remainingDamage <= 0) break;
                    
                    TestPermanent blocker = gameState.findPermanent(blockerName);
                    if (blocker == null) continue;
                    
                    int blockerToughness = blocker.toughness + blocker.toughnessModifier + 
                                          blocker.plusCounters - blocker.minusCounters;
                    int lethalDamage = hasDeathtouch ? 1 : Math.max(0, blockerToughness - blocker.damageMarked);
                    int assignedDamage = Math.min(remainingDamage, lethalDamage);
                    
                    if (assignedDamage > 0) {
                        Map<String, Object> dmgEvent = new LinkedHashMap<>();
                        dmgEvent.put("source", attackerName);
                        dmgEvent.put("target", blockerName);
                        dmgEvent.put("amount", assignedDamage);
                        dmgEvent.put("type", "combat_damage");
                        dmgEvent.put("deathtouch", hasDeathtouch);
                        damageEvents.add(dmgEvent);
                        
                        blocker.damageMarked += assignedDamage;
                        remainingDamage -= assignedDamage;
                    }
                }
                
                // Trample excess
                if (hasTrample && remainingDamage > 0) {
                    String defender = attacker.controller.equals("playerA") ? "playerB" : "playerA";
                    
                    Map<String, Object> dmgEvent = new LinkedHashMap<>();
                    dmgEvent.put("source", attackerName);
                    dmgEvent.put("target", defender);
                    dmgEvent.put("amount", remainingDamage);
                    dmgEvent.put("type", "trample_damage");
                    damageEvents.add(dmgEvent);
                    
                    gameState.playerLife.merge(defender, -remainingDamage, Integer::sum);
                }
                
                int totalDamageDealt = attackerPower;
                if (attackerKeywords.contains("Lifelink")) {
                    lifelinkGains.merge(attacker.controller, totalDamageDealt, Integer::sum);
                }
            }
        }
        
        // Apply lifelink
        for (Map.Entry<String, Integer> entry : lifelinkGains.entrySet()) {
            gameState.playerLife.merge(entry.getKey(), entry.getValue(), Integer::sum);
            
            Map<String, Object> lifeEvent = new LinkedHashMap<>();
            lifeEvent.put("player", entry.getKey());
            lifeEvent.put("amount", entry.getValue());
            lifeEvent.put("type", "lifelink");
            damageEvents.add(lifeEvent);
        }
        
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("step", damageStep);
        result.put("events", damageEvents);
        result.put("newState", gameState.toJson());
        
        return successResponse(result);
    }
    
    /**
     * Get keyword abilities for a card
     */
    private JsonObject handleKeywords(JsonObject request) {
        String cardName = request.get("card").getAsString();
        
        CardInfo cardInfo = cardRepo.findCard(cardName);
        if (cardInfo == null) {
            return errorResponse("card_not_found", "No card found: " + cardName);
        }
        
        Card card = cardInfo.createMockCard();
        List<String> keywords = getKeywords(card);
        
        // Categorize keywords
        Map<String, List<String>> categorized = new LinkedHashMap<>();
        categorized.put("combat", new ArrayList<>());
        categorized.put("evasion", new ArrayList<>());
        categorized.put("protection", new ArrayList<>());
        categorized.put("triggered", new ArrayList<>());
        categorized.put("static", new ArrayList<>());
        categorized.put("other", new ArrayList<>());
        
        Set<String> combatKeywords = Set.of("First strike", "Double strike", "Deathtouch", 
            "Lifelink", "Trample", "Vigilance", "Menace", "Haste");
        Set<String> evasionKeywords = Set.of("Flying", "Reach", "Shadow", "Horsemanship",
            "Fear", "Intimidate", "Skulk", "Landwalk");
        Set<String> protectionKeywords = Set.of("Hexproof", "Shroud", "Indestructible", "Ward");
        
        for (String kw : keywords) {
            if (combatKeywords.contains(kw)) {
                categorized.get("combat").add(kw);
            } else if (evasionKeywords.contains(kw) || kw.startsWith("Protection")) {
                categorized.get("evasion").add(kw);
            } else if (protectionKeywords.contains(kw)) {
                categorized.get("protection").add(kw);
            } else if (kw.contains("Cascade") || kw.contains("Annihilator") || 
                      kw.contains("Landfall") || kw.contains("Prowess")) {
                categorized.get("triggered").add(kw);
            } else if (kw.contains("Defender") || kw.contains("Changeling")) {
                categorized.get("static").add(kw);
            } else {
                categorized.get("other").add(kw);
            }
        }
        
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("card", cardName);
        result.put("keywords", keywords);
        result.put("categorized", categorized);
        
        return successResponse(result);
    }
    
    // =========================================================================
    // Validation Helpers
    // =========================================================================
    
    private Map<String, Object> validateCast(String cardName, TestGameState state) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("legal", false);
        
        CardInfo cardInfo = cardRepo.findCard(cardName);
        if (cardInfo == null) {
            result.put("reason", "Card not found");
            return result;
        }
        
        Card card = cardInfo.createMockCard();
        
        // Check if in hand
        String activePlayer = state.activePlayer;
        List<String> hand = state.hands.getOrDefault(activePlayer, new ArrayList<>());
        if (!hand.contains(cardName)) {
            result.put("reason", "Card not in hand");
            return result;
        }
        
        // Check mana (simplified - just CMC for now)
        int availableMana = state.getAvailableMana(activePlayer);
        int manaCost = (int) card.getManaValue();
        if (availableMana < manaCost) {
            result.put("reason", "Not enough mana");
            result.put("required", manaCost);
            result.put("available", availableMana);
            return result;
        }
        
        // Check sorcery speed
        boolean isInstant = card.getCardType().contains(CardType.INSTANT);
        boolean hasFlash = getKeywords(card).contains("Flash");
        if (!isInstant && !hasFlash && !state.phase.equals("main1") && !state.phase.equals("main2")) {
            result.put("reason", "Can only cast at sorcery speed");
            return result;
        }
        
        result.put("legal", true);
        return result;
    }
    
    private Map<String, Object> validateActivate(String cardName, TestGameState state) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("legal", false);

        // 1. Check permanent exists on battlefield
        TestPermanent permanent = state.findPermanent(cardName);
        if (permanent == null) {
            result.put("reason", "Permanent not on battlefield");
            return result;
        }

        // 2. Check controller
        if (!permanent.controller.equals(state.activePlayer)) {
            result.put("reason", "Not your permanent");
            return result;
        }

        // 3. Look up card in XMage DB for activated abilities
        CardInfo cardInfo = cardRepo.findCard(cardName);
        if (cardInfo == null) {
            // Card not in DB — can't validate, allow it (graceful degradation)
            result.put("legal", true);
            result.put("reason", "Card not in database, allowing activation");
            return result;
        }

        Card card = cardInfo.createMockCard();
        List<Map<String, Object>> activatedAbilities = new ArrayList<>();

        for (Ability ability : card.getAbilities()) {
            String rule = ability.getRule();
            if (rule == null || rule.isEmpty()) continue;

            String className = ability.getClass().getSimpleName();

            // Filter to activated abilities:
            // - Class name contains "Activated" (SimpleActivatedAbility, etc.)
            // - Or it's a loyalty ability (planeswalker)
            // - Or rule text follows "cost: effect" pattern for battlefield abilities
            boolean isActivated = className.contains("Activated")
                || className.contains("LoyaltyAbility")
                || (rule.contains(": ") && ability.getZone() == Zone.BATTLEFIELD
                    && !className.contains("Triggered") && !className.contains("Static"));

            if (!isActivated) continue;

            Map<String, Object> abilityInfo = new LinkedHashMap<>();
            abilityInfo.put("rule", rule);
            abilityInfo.put("type", className);

            boolean canActivate = true;
            List<String> reasons = new ArrayList<>();

            // Extract cost part (before the colon)
            String costPart = "";
            int colonIdx = rule.indexOf(':');
            if (colonIdx > 0) {
                costPart = rule.substring(0, colonIdx);
            }

            // Check tap cost
            boolean requiresTap = costPart.contains("{T}");
            abilityInfo.put("requiresTap", requiresTap);
            if (requiresTap) {
                if (permanent.tapped) {
                    canActivate = false;
                    reasons.add("Permanent is tapped");
                }
                // Summoning sickness: can't use {T} abilities if summoning sick (unless haste)
                if (permanent.summoningSick) {
                    Set<String> keywords = getKeywordsFromState(permanent, state);
                    boolean hasHaste = keywords.stream()
                        .anyMatch(k -> k.equalsIgnoreCase("Haste"));
                    if (!hasHaste) {
                        canActivate = false;
                        reasons.add("Summoning sickness (no haste)");
                    }
                }
            }

            // Parse mana cost from cost part
            // e.g. "{2}{R}, {T}: Deal 1 damage..." → cost is "{2}{R}, {T}"
            int manaCost = 0;
            java.util.regex.Pattern manaPattern = java.util.regex.Pattern.compile("\\{(\\d+)\\}");
            java.util.regex.Matcher matcher = manaPattern.matcher(costPart);
            while (matcher.find()) {
                manaCost += Integer.parseInt(matcher.group(1));
            }
            // Count colored mana symbols in cost
            for (String color : new String[]{"W", "U", "B", "R", "G"}) {
                String search = "{" + color + "}";
                int idx = 0;
                while ((idx = costPart.indexOf(search, idx)) != -1) {
                    manaCost++;
                    idx += search.length();
                }
            }

            abilityInfo.put("manaCost", manaCost);
            if (manaCost > 0) {
                int availableMana = state.getAvailableMana(state.activePlayer);
                if (availableMana < manaCost) {
                    canActivate = false;
                    reasons.add("Not enough mana (need " + manaCost + ", have " + availableMana + ")");
                }
            }

            // Check sorcery speed restriction
            String ruleLower = rule.toLowerCase();
            boolean sorcerySpeed = ruleLower.contains("activate only as a sorcery")
                || ruleLower.contains("activate this ability only any time you could cast a sorcery");
            abilityInfo.put("sorcerySpeed", sorcerySpeed);
            if (sorcerySpeed) {
                if (!state.phase.equals("main1") && !state.phase.equals("main2")) {
                    canActivate = false;
                    reasons.add("Can only activate as a sorcery (not in main phase)");
                }
                // Check stack empty for sorcery speed
                if (state.stackSize > 0) {
                    canActivate = false;
                    reasons.add("Stack must be empty for sorcery-speed activation");
                }
            }

            // Check once-per-turn restriction
            boolean oncePerTurn = ruleLower.contains("activate only once")
                || ruleLower.contains("activate this ability only once");
            abilityInfo.put("oncePerTurn", oncePerTurn);
            // Note: actual tracking of whether it was already used this turn
            // is handled by the Python side (we report the restriction exists)

            abilityInfo.put("canActivate", canActivate);
            if (!reasons.isEmpty()) {
                abilityInfo.put("reasons", reasons);
            }

            activatedAbilities.add(abilityInfo);
        }

        result.put("abilities", activatedAbilities);

        // Legal if at least one ability can be activated
        boolean anyLegal = false;
        for (Map<String, Object> a : activatedAbilities) {
            if ((boolean) a.get("canActivate")) {
                anyLegal = true;
                break;
            }
        }
        result.put("legal", anyLegal);

        if (!anyLegal && !activatedAbilities.isEmpty()) {
            result.put("reason", "No activated abilities can be used right now");
        } else if (activatedAbilities.isEmpty()) {
            // No activated abilities found in DB — allow (graceful degradation)
            result.put("legal", true);
            result.put("reason", "No activated abilities found in database");
        }

        return result;
    }
    
    private Map<String, Object> validateAttack(String cardName, TestGameState state) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("legal", false);
        
        TestPermanent creature = state.findPermanent(cardName);
        if (creature == null) {
            result.put("reason", "Creature not on battlefield");
            return result;
        }
        
        if (!creature.isCreature) {
            result.put("reason", "Not a creature");
            return result;
        }
        
        if (!creature.controller.equals(state.activePlayer)) {
            result.put("reason", "Not your creature");
            return result;
        }
        
        if (creature.tapped) {
            result.put("reason", "Creature is tapped");
            return result;
        }
        
        if (creature.summoningSick) {
            Set<String> keywords = getKeywordsFromState(creature, state);
            if (!keywords.contains("Haste")) {
                result.put("reason", "Summoning sickness");
                return result;
            }
        }
        
        // Check for Defender
        Set<String> keywords = getKeywordsFromState(creature, state);
        if (keywords.contains("Defender")) {
            result.put("reason", "Has defender");
            return result;
        }
        
        result.put("legal", true);
        return result;
    }
    
    private Map<String, Object> validateBlock(String blockerName, JsonObject request, TestGameState state) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("legal", false);
        
        String attackerName = request.has("attacker") ? request.get("attacker").getAsString() : null;
        
        TestPermanent blocker = state.findPermanent(blockerName);
        if (blocker == null) {
            result.put("reason", "Blocker not on battlefield");
            return result;
        }
        
        if (blocker.tapped) {
            result.put("reason", "Blocker is tapped");
            return result;
        }
        
        if (attackerName != null) {
            TestPermanent attacker = state.findPermanent(attackerName);
            if (attacker != null) {
                Set<String> attackerKeywords = getKeywordsFromState(attacker, state);
                Set<String> blockerKeywords = getKeywordsFromState(blocker, state);
                
                // Flying check
                if (attackerKeywords.contains("Flying") && 
                    !blockerKeywords.contains("Flying") && 
                    !blockerKeywords.contains("Reach")) {
                    result.put("reason", "Can't block flyer without flying or reach");
                    return result;
                }
                
                // Shadow
                if (attackerKeywords.contains("Shadow") && !blockerKeywords.contains("Shadow")) {
                    result.put("reason", "Can't block shadow without shadow");
                    return result;
                }
            }
        }
        
        result.put("legal", true);
        return result;
    }
    
    // =========================================================================
    // Card Property Extraction
    // =========================================================================
    
    private List<String> getTypes(Card card) {
        List<String> types = new ArrayList<>();
        for (CardType type : card.getCardType()) {
            types.add(type.toString());
        }
        return types;
    }
    
    private List<String> getSubtypes(Card card) {
        List<String> subtypes = new ArrayList<>();
        for (SubType subtype : card.getSubtype()) {
            subtypes.add(subtype.toString());
        }
        return subtypes;
    }
    
    private List<String> getSupertypes(Card card) {
        List<String> supertypes = new ArrayList<>();
        for (SuperType supertype : card.getSuperType()) {
            supertypes.add(supertype.toString());
        }
        return supertypes;
    }
    
    private List<String> getColors(Card card) {
        List<String> colors = new ArrayList<>();
        if (card.getColor().isWhite()) colors.add("W");
        if (card.getColor().isBlue()) colors.add("U");
        if (card.getColor().isBlack()) colors.add("B");
        if (card.getColor().isRed()) colors.add("R");
        if (card.getColor().isGreen()) colors.add("G");
        return colors;
    }
    
    private List<String> getKeywords(Card card) {
        List<String> keywords = new ArrayList<>();
        for (Ability ability : card.getAbilities()) {
            String rule = ability.getRule();
            // Extract keyword abilities
            String[] commonKeywords = {
                "Flying", "First strike", "Double strike", "Deathtouch",
                "Haste", "Hexproof", "Indestructible", "Lifelink",
                "Menace", "Reach", "Trample", "Vigilance", "Defender",
                "Flash", "Shroud", "Fear", "Intimidate", "Wither",
                "Infect", "Undying", "Persist", "Cascade", "Prowess",
                "Shadow", "Horsemanship", "Changeling", "Devoid"
            };
            
            for (String keyword : commonKeywords) {
                if (rule.toLowerCase().startsWith(keyword.toLowerCase())) {
                    keywords.add(keyword);
                }
            }
            
            // Protection
            if (rule.toLowerCase().startsWith("protection from")) {
                keywords.add(rule);
            }
        }
        return keywords;
    }
    
    private List<Map<String, Object>> getAbilities(Card card) {
        List<Map<String, Object>> abilities = new ArrayList<>();
        for (Ability ability : card.getAbilities()) {
            Map<String, Object> abilityData = new LinkedHashMap<>();
            abilityData.put("type", ability.getClass().getSimpleName());
            abilityData.put("rule", ability.getRule());
            abilityData.put("zone", ability.getZone().toString());
            abilities.add(abilityData);
        }
        return abilities;
    }
    
    private Set<String> getKeywordsFromState(TestPermanent perm, TestGameState state) {
        Set<String> keywords = new HashSet<>(perm.keywords);
        
        // Look up card for base keywords if needed
        if (keywords.isEmpty()) {
            CardInfo cardInfo = cardRepo.findCard(perm.name);
            if (cardInfo != null) {
                keywords.addAll(getKeywords(cardInfo.createMockCard()));
            }
        }
        
        return keywords;
    }
    
    // =========================================================================
    // Response Helpers
    // =========================================================================
    
    private JsonObject successResponse(Object data) {
        JsonObject response = new JsonObject();
        response.addProperty("success", true);
        response.add("data", gson.toJsonTree(data));
        return response;
    }
    
    private JsonObject errorResponse(String code, String message) {
        JsonObject response = new JsonObject();
        response.addProperty("success", false);
        response.addProperty("error", code);
        response.addProperty("message", message);
        return response;
    }
    
    // =========================================================================
    // Game State Parsing
    // =========================================================================
    
    private TestGameState parseGameState(JsonObject json) {
        TestGameState state = new TestGameState();
        
        if (json.has("activePlayer")) {
            state.activePlayer = json.get("activePlayer").getAsString();
        }
        
        if (json.has("phase")) {
            state.phase = json.get("phase").getAsString();
        }

        if (json.has("stackSize")) {
            state.stackSize = json.get("stackSize").getAsInt();
        }

        if (json.has("life")) {
            JsonObject lifeJson = json.getAsJsonObject("life");
            for (String player : lifeJson.keySet()) {
                state.playerLife.put(player, lifeJson.get(player).getAsInt());
            }
        }
        
        if (json.has("battlefield")) {
            JsonArray bfJson = json.getAsJsonArray("battlefield");
            for (JsonElement el : bfJson) {
                state.battlefield.add(TestPermanent.fromJson(el.getAsJsonObject()));
            }
        }
        
        if (json.has("hands")) {
            JsonObject handsJson = json.getAsJsonObject("hands");
            for (String player : handsJson.keySet()) {
                List<String> hand = new ArrayList<>();
                for (JsonElement card : handsJson.getAsJsonArray(player)) {
                    hand.add(card.getAsString());
                }
                state.hands.put(player, hand);
            }
        }
        
        if (json.has("lands")) {
            JsonObject landsJson = json.getAsJsonObject("lands");
            for (String player : landsJson.keySet()) {
                state.untappedLands.put(player, landsJson.get(player).getAsInt());
            }
        }
        
        return state;
    }
    
    // =========================================================================
    // Test Game State Classes
    // =========================================================================
    
    static class TestGameState {
        String activePlayer = "playerA";
        String phase = "main1";
        int stackSize = 0;
        Map<String, Integer> playerLife = new HashMap<>();
        Map<String, Integer> poisonCounters = new HashMap<>();
        List<TestPermanent> battlefield = new ArrayList<>();
        Map<String, List<String>> hands = new HashMap<>();
        Map<String, List<String>> graveyards = new HashMap<>();
        Map<String, Integer> untappedLands = new HashMap<>();
        
        TestGameState() {
            playerLife.put("playerA", 20);
            playerLife.put("playerB", 20);
            poisonCounters.put("playerA", 0);
            poisonCounters.put("playerB", 0);
        }
        
        TestPermanent findPermanent(String name) {
            for (TestPermanent p : battlefield) {
                if (p.name.equals(name)) return p;
            }
            return null;
        }
        
        int getAvailableMana(String player) {
            return untappedLands.getOrDefault(player, 0);
        }
        
        JsonObject toJson() {
            JsonObject json = new JsonObject();
            json.addProperty("activePlayer", activePlayer);
            json.addProperty("phase", phase);
            json.addProperty("stackSize", stackSize);
            json.add("life", new Gson().toJsonTree(playerLife));
            json.add("poison", new Gson().toJsonTree(poisonCounters));
            
            JsonArray bfArray = new JsonArray();
            for (TestPermanent p : battlefield) {
                bfArray.add(p.toJson());
            }
            json.add("battlefield", bfArray);
            
            json.add("hands", new Gson().toJsonTree(hands));
            json.add("graveyards", new Gson().toJsonTree(graveyards));
            json.add("lands", new Gson().toJsonTree(untappedLands));
            
            return json;
        }
    }
    
    static class TestPermanent {
        String name;
        String controller;
        boolean isCreature;
        boolean isLegendary;
        int power;
        int toughness;
        int powerModifier;
        int toughnessModifier;
        int plusCounters;
        int minusCounters;
        int damageMarked;
        boolean tapped;
        boolean summoningSick;
        Set<String> keywords = new HashSet<>();
        
        static TestPermanent fromJson(JsonObject json) {
            TestPermanent p = new TestPermanent();
            p.name = json.has("name") ? json.get("name").getAsString() : "";
            p.controller = json.has("controller") ? json.get("controller").getAsString() : "playerA";
            p.isCreature = json.has("isCreature") ? json.get("isCreature").getAsBoolean() : false;
            p.isLegendary = json.has("isLegendary") ? json.get("isLegendary").getAsBoolean() : false;
            p.power = json.has("power") ? json.get("power").getAsInt() : 0;
            p.toughness = json.has("toughness") ? json.get("toughness").getAsInt() : 0;
            p.powerModifier = json.has("powerModifier") ? json.get("powerModifier").getAsInt() : 0;
            p.toughnessModifier = json.has("toughnessModifier") ? json.get("toughnessModifier").getAsInt() : 0;
            p.plusCounters = json.has("plusCounters") ? json.get("plusCounters").getAsInt() : 0;
            p.minusCounters = json.has("minusCounters") ? json.get("minusCounters").getAsInt() : 0;
            p.damageMarked = json.has("damageMarked") ? json.get("damageMarked").getAsInt() : 0;
            p.tapped = json.has("tapped") ? json.get("tapped").getAsBoolean() : false;
            p.summoningSick = json.has("summoningSick") ? json.get("summoningSick").getAsBoolean() : true;
            
            if (json.has("keywords")) {
                for (JsonElement kw : json.getAsJsonArray("keywords")) {
                    p.keywords.add(kw.getAsString());
                }
            }
            
            return p;
        }
        
        JsonObject toJson() {
            JsonObject json = new JsonObject();
            json.addProperty("name", name);
            json.addProperty("controller", controller);
            json.addProperty("isCreature", isCreature);
            json.addProperty("isLegendary", isLegendary);
            json.addProperty("power", power);
            json.addProperty("toughness", toughness);
            json.addProperty("powerModifier", powerModifier);
            json.addProperty("toughnessModifier", toughnessModifier);
            json.addProperty("plusCounters", plusCounters);
            json.addProperty("minusCounters", minusCounters);
            json.addProperty("damageMarked", damageMarked);
            json.addProperty("tapped", tapped);
            json.addProperty("summoningSick", summoningSick);
            json.add("keywords", new Gson().toJsonTree(keywords));
            return json;
        }
    }
}
