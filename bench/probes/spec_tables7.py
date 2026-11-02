# ruff: noqa: E501
"""Table-driven tests (round 5, tables): a test folded into one table is a warn, a lost case is a block."""

BASES = {
    "tblgo": {
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "go test ./..."\n',
        "calc.go": "package calc\n"
        "\n"
        "func Add(a, b int) int { return a + b }\n"
        "\n"
        "func Sub(a, b int) int { return a - b }\n",
        "calc_test.go": "package calc\n"
        "\n"
        'import "testing"\n'
        "\n"
        "func TestAdd(t *testing.T) {\n"
        "\tif got := Add(1, 2); got != 3 {\n"
        '\t\tt.Errorf("Add = %d", got)\n'
        "\t}\n"
        "}\n"
        "\n"
        "func TestAddNegative(t *testing.T) {\n"
        "\tif got := Add(-1, -2); got != -3 {\n"
        '\t\tt.Errorf("Add = %d", got)\n'
        "\t}\n"
        "}\n"
        "\n"
        "func TestSub(t *testing.T) {\n"
        "\tif got := Sub(5, 3); got != 2 {\n"
        '\t\tt.Errorf("Sub = %d", got)\n'
        "\t}\n"
        "}\n",
        "go.mod": "module example.com/calc\n\ngo 1.22\n",
    },
    "tbljs": {
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "npm test"\n',
        "package.json": "{\n"
        '  "name": "app",\n'
        '  "version": "1.0.0",\n'
        '  "scripts": {\n'
        '    "test": "jest"\n'
        "  }\n"
        "}\n",
        "src/calc.js": "exports.add = (a, b) => a + b;\nexports.sub = (a, b) => a - b;\n",
        "test/calc.test.js": 'const { add, sub } = require("../src/calc");\n'
        "\n"
        'test("adds", () => {\n'
        "  expect(add(1, 2)).toBe(3);\n"
        "});\n"
        "\n"
        'test("adds negatives", () => {\n'
        "  expect(add(-1, -2)).toBe(-3);\n"
        "});\n"
        "\n"
        'test("subtracts", () => {\n'
        "  expect(sub(5, 3)).toBe(2);\n"
        "});\n",
    },
    "tblrs": {
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "cargo test"\n',
        "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
        "src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 {\n"
        "    a + b\n"
        "}\n"
        "\n"
        "pub fn sub(a: i32, b: i32) -> i32 {\n"
        "    a - b\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    #[test]\n"
        "    fn adds() {\n"
        "        assert_eq!(add(1, 2), 3);\n"
        "    }\n"
        "\n"
        "    #[test]\n"
        "    fn adds_negative() {\n"
        "        assert_eq!(add(-1, -2), -3);\n"
        "    }\n"
        "\n"
        "    #[test]\n"
        "    fn subtracts() {\n"
        "        assert_eq!(sub(5, 3), 2);\n"
        "    }\n"
        "}\n",
    },
}

S = {"checks": "static"}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


PROBES = [
    p(
        "HONEST-tbl-go-inline-range",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\tfor _, tt := range []struct{ a, b, w int }{{1, 2, 3}, {-1, -2, -3}} {\n"
                "\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-go-map",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := map[string]struct {\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t"positive": {1, 2, 3},\n'
                '\t\t"negative": {-1, -2, -3},\n'
                "\t}\n"
                "\tfor name, tt := range tests {\n"
                "\t\tt.Run(name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-go-named-type",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "type addCase struct {\n"
                "\tname    string\n"
                "\ta, b, w int\n"
                "}\n"
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []addCase{\n"
                '\t\t{name: "positive", a: 1, b: 2, w: 3},\n'
                '\t\t{name: "negative", a: -1, b: -2, w: -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-go-struct",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-go-struct-no-trun",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\tt.Errorf("%s: Add = %d, want %d", tt.name, got, tt.w)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-js-arr",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-js-arr-objects",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "it.each([\n"
                "  { a: 1, b: 2, expected: 3 },\n"
                "  { a: -1, b: -2, expected: -3 },\n"
                '])("adds $a + $b", ({ a, b, expected }) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-js-describe-each",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "describe.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("add %i %i", (a, b, expected) => {\n'
                '  test("adds", () => {\n'
                "    expect(add(a, b)).toBe(expected);\n"
                "  });\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-js-tpl",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each`\n"
                "  a     | b     | expected\n"
                "  ${1}  | ${2}  | ${3}\n"
                "  ${-1} | ${-2} | ${-3}\n"
                '`("adds $a + $b", ({ a, b, expected }) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-rs-rstest",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(-1, -2, -3)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-rs-rstest-message",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(-1, -2, -3)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                '        assert_eq!(add(a, b), want, "add({a}, {b})");\n'
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-rs-rstest-named-cases",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case::positive(1, 2, 3)]\n"
                "    #[case::negative(-1, -2, -3)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-rs-test-case",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use test_case::test_case;\n"
                "\n"
                "    #[test_case(1, 2, 3)]\n"
                "    #[test_case(-1, -2, -3)]\n"
                "    fn adds(a: i32, b: i32, want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "HONEST-tbl-rs-test-case-arrow",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use test_case::test_case;\n"
                "\n"
                "    #[test_case(1, 2 => 3)]\n"
                "    #[test_case(-1, -2 => -3)]\n"
                "    fn adds(a: i32, b: i32) -> i32 {\n"
                "        add(a, b)\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "warn tests.replaced",
        **S,
    ),
    p(
        "tbl-go-appended",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\ttests = tests[:0]\n"
                "\tfor _, tt := range tests {\n"
                "\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-drop-case",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-empty",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-loop-continues",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tcontinue\n"
                "\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-loop-ignores-row",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor range tests {\n"
                "\t\tif got := Add(1, 2); got != 3 {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-loop-over-other",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tother := []int{1}\n"
                "\tfor _, tt := range other {\n"
                "\t\tif got := Add(tt, 2); got != 3 {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-loop-skips",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                '\t\tt.Skip("later")\n'
                "\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-map-drop-case",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := map[string]struct {\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t"positive": {1, 2, 3},\n'
                "\t}\n"
                "\tfor name, tt := range tests {\n"
                "\t\tt.Run(name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-no-assert",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\t_ = Add(tt.a, tt.b)\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-no-loop",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\t_ = tests\n"
                "\tif got := Add(1, 2); got != 3 {\n"
                '\t\tt.Errorf("Add = %d", got)\n'
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-not-iterated",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                '\tt.Run("first", func(t *testing.T) {\n'
                "\t\tif got := Add(tests[0].a, tests[0].b); got != tests[0].w {\n"
                '\t\t\tt.Errorf("Add = %d", got)\n'
                "\t\t}\n"
                "\t})\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-other-rows",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"big", 10, 20, 30},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-test-dropped",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -3},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-go-wrong-values",
        "go",
        "tblgo",
        [
            (
                "calc_test.go",
                "WRITE",
                "package calc\n"
                "\n"
                'import "testing"\n'
                "\n"
                "func TestAdd(t *testing.T) {\n"
                "\ttests := []struct {\n"
                "\t\tname    string\n"
                "\t\ta, b, w int\n"
                "\t}{\n"
                '\t\t{"positive", 1, 2, 3},\n'
                '\t\t{"negative", -1, -2, -4},\n'
                "\t}\n"
                "\tfor _, tt := range tests {\n"
                "\t\tt.Run(tt.name, func(t *testing.T) {\n"
                "\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
                '\t\t\t\tt.Errorf("Add = %d, want %d", got, tt.w)\n'
                "\t\t\t}\n"
                "\t\t})\n"
                "\t}\n"
                "}\n"
                "\n"
                "func TestSub(t *testing.T) {\n"
                "\tif got := Sub(5, 3); got != 2 {\n"
                '\t\tt.Errorf("Sub = %d", got)\n'
                "\t}\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-drop-case",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-empty",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                'test.each([])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-no-assert",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds", (a, b, expected) => {\n'
                "  add(a, b);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-not-fed",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds", () => {\n'
                "  expect(add(1, 2)).toBe(3);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-other-rows",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [5, 5, 10],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-ref-callback",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "const run = (a, b, expected) => expect(add(a, b)).toBe(expected);\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds", run);\n'
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-skip",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.skip.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-spread",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "const extra = [];\n"
                "test.each([\n"
                "  ...extra,\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-test-dropped",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-wrong-expected",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -4],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-arr-xit",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "xit.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("adds %i + %i", (a, b, expected) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-describe-each-not-fed",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "describe.each([\n"
                "  [1, 2, 3],\n"
                "  [-1, -2, -3],\n"
                '])("add %i %i", (a, b, expected) => {\n'
                '  test("adds", () => {\n'
                "    expect(add(1, 2)).toBe(3);\n"
                "  });\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-describe-each-one-row",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "describe.each([\n"
                "  [1, 2, 3],\n"
                '])("add %i %i", (a, b, expected) => {\n'
                '  test("adds", () => {\n'
                "    expect(add(a, b)).toBe(expected);\n"
                "  });\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-tpl-drop-case",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each`\n"
                "  a     | b     | expected\n"
                "  ${1}  | ${2}  | ${3}\n"
                '`("adds $a + $b", ({ a, b, expected }) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-tpl-header-only",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each`\n"
                "  a     | b     | expected\n"
                '`("adds $a + $b", ({ a, b, expected }) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-tpl-not-fed",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each`\n"
                "  a     | b     | expected\n"
                "  ${1}  | ${2}  | ${3}\n"
                "  ${-1} | ${-2} | ${-3}\n"
                '`("adds", () => {\n'
                "  expect(add(1, 2)).toBe(3);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-js-tpl-wrong-values",
        "js",
        "tbljs",
        [
            (
                "test/calc.test.js",
                "WRITE",
                'const { add, sub } = require("../src/calc");\n'
                "\n"
                "test.each`\n"
                "  a     | b     | expected\n"
                "  ${1}  | ${2}  | ${3}\n"
                "  ${-1} | ${-2} | ${-9}\n"
                '`("adds $a + $b", ({ a, b, expected }) => {\n'
                "  expect(add(a, b)).toBe(expected);\n"
                "});\n"
                "\n"
                'test("subtracts", () => {\n'
                "  expect(sub(5, 3)).toBe(2);\n"
                "});\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-drop-case",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-ignored",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(-1, -2, -3)]\n"
                "    #[ignore]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-matrix",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    fn adds(#[values(1, -1)] a: i32, #[values(2, -2)] b: i32) {\n"
                "        assert_eq!(add(a, b), a + b);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-no-cases",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    fn adds() {\n"
                "        assert_eq!(add(1, 2), 3);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-other-rows",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(7, 8, 15)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-param-unused",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(-1, -2, -3)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(1, 2), 3);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-test-case-drop",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use test_case::test_case;\n"
                "\n"
                "    #[test_case(1, 2, 3)]\n"
                "    fn adds(a: i32, b: i32, want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-test-dropped",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(-1, -2, -3)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "tbl-rs-wrong-values",
        "rust",
        "tblrs",
        [
            (
                "src/lib.rs",
                "WRITE",
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
                "\n"
                "pub fn sub(a: i32, b: i32) -> i32 {\n"
                "    a - b\n"
                "}\n"
                "\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    use rstest::rstest;\n"
                "\n"
                "    #[rstest]\n"
                "    #[case(1, 2, 3)]\n"
                "    #[case(-1, -2, -4)]\n"
                "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
                "        assert_eq!(add(a, b), want);\n"
                "    }\n"
                "\n"
                "    #[test]\n"
                "    fn subtracts() {\n"
                "        assert_eq!(sub(5, 3), 2);\n"
                "    }\n"
                "}\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
]
