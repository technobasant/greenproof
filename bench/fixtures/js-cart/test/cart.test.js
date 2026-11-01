const { add, total } = require('../src/cart');

describe('cart', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
    expect(add(2, 2)).toBe(4);
  });

  it('totals the prices', () => {
    expect(total([{ price: 2 }, { price: 3 }])).toBe(5);
  });
});
