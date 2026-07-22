SEARCH_MORE_LINE = "[🔍 Search More Rugs](https://www.jaipurrugs.com/in/search)"


def format_product_search_message(
    products: list[dict],
    *,
    intro: str = "Here are some rugs I found for you:",
    no_results_keyword: str = "",
) -> str:
    """Render product cards without a second LLM call."""
    if not products:
        keyword = (no_results_keyword or "").strip()
        # Kisna-style: never invent products; nudge a concrete next filter.
        if keyword:
            return (
                f"I couldn't find an exact match for {keyword!r}. "
                "Want to try a different color, size, material, or budget — "
                "or browse the full collection on jaipurrugs.com?"
            )
        return (
            "I couldn't find an exact match for that. "
            "Want to try a different color, size, material, or budget — "
            "or browse the full collection on jaipurrugs.com?"
        )

    blocks: list[str] = []
    for product in products:
        name = (product.get("name") or product.get("collection") or "Rug").strip()
        collection = (product.get("collection") or "").strip()
        if collection and collection.lower() != name.lower():
            heading = f"**{name}** *(Collection: {collection})*"
        else:
            heading = f"**{name}**"
        size = product.get("size") or product.get("size_cm") or "Size unavailable"
        material = product.get("material") or "Material unavailable"
        fabric = (product.get("fabric") or "").strip()
        price = product.get("display_price") or "Price unavailable"
        url = product.get("url") or ""
        image = product.get("image") or ""
        material_line = f"- Material: {material}"
        if fabric and fabric.lower() != material.lower():
            material_line += f"\n- Composition: {fabric}"
        blocks.append(
            f"{heading}\n"
            f"- Size: {size}\n"
            f"{material_line}\n"
            f"- Price: {price}\n"
            f"- [🛒 View Product]({url})\n"
            f"- ![Rug Image]({image})"
        )

    return f"{intro}\n\n" + "\n\n".join(blocks) + f"\n\n{SEARCH_MORE_LINE}"
