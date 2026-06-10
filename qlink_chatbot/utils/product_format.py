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
        if keyword:
            return (
                f"I couldn't find any rugs matching {keyword!r}. "
                "Would you like to try a different color, size, or style?"
            )
        return (
            "I couldn't find any rugs matching that search. "
            "Would you like to try a different color, size, or style?"
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
        price = product.get("display_price") or "Price unavailable"
        url = product.get("url") or ""
        image = product.get("image") or ""
        blocks.append(
            f"{heading}\n"
            f"- Size: {size}\n"
            f"- Material: {material}\n"
            f"- Price: {price}\n"
            f"- [🛒 View Product]({url})\n"
            f"- ![Rug Image]({image})"
        )

    return f"{intro}\n\n" + "\n\n".join(blocks) + f"\n\n{SEARCH_MORE_LINE}"
