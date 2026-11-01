function add(a, b) {
  return a + b;
}

function total(items) {
  return items.reduce((sum, item) => sum + item.price, 0);
}

module.exports = { add, total };
