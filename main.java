public class Main {
    static int square(int n) {
        return n * n;
    }

    public static void main(String[] args) {
        Point p = new Point(3, 4);
        int total = 0;
        for (int i = 1; i <= 3; i++) {
            total += square(i);
        }
        System.out.println(total + p.sum());
    }
}

class Point {
    int x;
    int y;

    Point(int x, int y) {
        this.x = x;
        this.y = y;
    }

    int sum() {
        return x + y;
    }
}