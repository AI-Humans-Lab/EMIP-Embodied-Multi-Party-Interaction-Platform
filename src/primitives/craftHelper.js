async function failedCraftFeedback(bot, name, item, craftingTable) {
    const allRecipes = bot.recipesAll(item.id, null, true);
    if (!allRecipes.length) {
        throw new Error(`No crafting recipe exists for ${name}`);
    }
    const recipes = bot.recipesAll(item.id, null, craftingTable);
    if (!recipes.length) {
        throw new Error(`Cannot craft ${name}: a crafting table is required`);
    }
    const alternatives = [];
    for (const recipe of recipes) {
        const missing = [];
        for (const ingredient of recipe.delta) {
            if (ingredient.count >= 0) continue;
            const shortage = -ingredient.count - bot.inventory.count(ingredient.id, ingredient.metadata);
            if (shortage > 0) missing.push(`${shortage} ${mcData.items[ingredient.id].name}`);
        }
        const description = missing.join(", ");
        if (description && !alternatives.some(alternative => alternative.description === description)) {
            alternatives.push({ description, missingTypeCount: missing.length });
        }
    }
    if (!alternatives.length) {
        throw new Error(`Unexpected crafting state for ${name}: ingredients appear sufficient, but no usable recipe was found for an unknown reason`);
    }
    alternatives.sort((a, b) => a.missingTypeCount - b.missingTypeCount);
    if (alternatives.length === 1) {
        throw new Error(`Cannot craft ${name}: missing ingredients: (${alternatives[0].description})`);
    }
    const shown = alternatives.slice(0, 3);
    const remaining = alternatives.length - shown.length;
    const suffix = remaining > 0 ? `; ${remaining} other recipe alternative(s) omitted` : "";
    throw new Error(`Cannot craft ${name}: missing ingredients; satisfy one of these recipe alternatives: ${shown.map(alternative => `(${alternative.description})`).join(" OR ")}${suffix}`);
}
