#include <iostream>
#include <vector>
#include <random>
#include <thread>
#include <chrono>
#include <mutex>
#include <fstream>

#define MIN_RANDOM_INT 0
#define MAX_RANDOM_INT 1000
#define BUFFER_SIZE 500
#define DATA_SIZE 1000
#define T1_INTERVAL 5
#define T2_INTERVAL 10
#define T3_INTERVAL 20

class Buffer {
private:
    std::vector<int> data;
    int front;
    int rear;
    int capacity;
    int size;
    std::mutex mtx;

public:
    Buffer(int cap) : capacity(cap), front(-1), rear(-1), size(0), data(cap) {}

    bool isFull() const {
        return size == capacity;
    }

    bool isEmpty() const {
        return size == 0;
    }

    void enqueue(int value) {
        std::lock_guard<std::mutex> lock(mtx);
        if (isFull()) {
            std::cout << "Buffer is full. Cannot enqueue " << value << std::endl;
            return;
        }
        if (isEmpty()) {
            front = rear = 0;
        } else {
            rear = (rear + 1) % capacity;
        }
        data[rear] = value;
        size++;
        std::cout << "Enqueued: " << value << std::endl;
    }

    bool dequeue(int &value) {
        std::lock_guard<std::mutex> lock(mtx);
        if (isEmpty()) {
            std::cout << "Buffer is empty. Cannot dequeue." << std::endl;
            return false;
        }
        value = data[front];
        if (front == rear) {
            front = rear = -1;
        } else {
            front = (front + 1) % capacity;
        }
        size--;
        return true;
    }

    void display() {
        std::lock_guard<std::mutex> lock(mtx);
        if (isEmpty()) {
            std::cout << "Buffer is empty." << std::endl;
            return;
        }
        std::cout << "Buffer contents: ";
        for (int i = 0; i < size; ++i) {
            std::cout << data[(front + i) % capacity] << " ";
        }
        std::cout << std::endl;
    }
};

int randomInt() {
    static std::random_device rd;
    static std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(MIN_RANDOM_INT, MAX_RANDOM_INT);
    return dis(gen);
}

void callback1(Buffer &buffer) {
    int value = randomInt();
    buffer.enqueue(value);
    buffer.display();
}

void callback2(Buffer &buffer, int *data, int &data_index) {
    int range = int(T2_INTERVAL / T1_INTERVAL);
    for (int i = 0; i < range; ++i) {
        int value;
        if (buffer.dequeue(value)) {
            data[data_index] = value;
            data_index = (data_index + 1) % DATA_SIZE;
            std::cout << "Data successfully transferred: " << value << std::endl;
        }
    }
}

void displayDataLogToFile(int *data, int size) {
    std::ofstream out("data_log.dat");
    if (!out) {
        std::cerr << "Failed to open file for writing\n";
        return;
    }
    for (int i = 0; i < size; ++i) {
        out << data[i] << " ";
    }
    out << std::endl;
    out.close();
}

void timerInterrupt1(Buffer &buffer) {
    while (true) {
        std::this_thread::sleep_for(std::chrono::milliseconds(T1_INTERVAL));
        callback1(buffer);
    }
}

void timerInterrupt2(Buffer &buffer, int *data, int &data_index) {
    while (true) {
        std::this_thread::sleep_for(std::chrono::milliseconds(T2_INTERVAL));
        callback2(buffer, data, data_index);
    }
}

int main() {
    Buffer buffer(BUFFER_SIZE);
    int data[DATA_SIZE] = {0};
    int data_index = 0;

    std::thread t1(timerInterrupt1, std::ref(buffer));
    std::thread t2(timerInterrupt2, std::ref(buffer), data, std::ref(data_index));
    std::thread t3([&data]() {
        while (true) {
            std::this_thread::sleep_for(std::chrono::milliseconds(T3_INTERVAL));
            displayDataLogToFile(data, DATA_SIZE);
        }
    });

    t1.join();
    t2.join();
    t3.join();

    return 0;
}
