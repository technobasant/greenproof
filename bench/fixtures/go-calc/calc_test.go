package calc

import "testing"

func TestAdd(t *testing.T) {
	if got := Add(1, 2); got != 3 {
		t.Errorf("Add(1, 2) = %d, want 3", got)
	}
}

func TestSub(t *testing.T) {
	if got := Sub(3, 1); got != 2 {
		t.Errorf("Sub(3, 1) = %d, want 2", got)
	}
}
