#ifndef __MAT_H__
#define __MAT_H__

void init_mat(float *m);
void mat_multiply_to(float *c, float *a, float *b);
void mat_multiply(float *dst, float *src);
void move_xyz(float *m, float x, float y, float z);
void scale_xyz(float *m, float x, float y, float z);
void rotate_x(float *m, float degree);
void rotate_y(float *m, float degree);
void rotate_z(float *m, float degree);
void frustumM(float *m,
              int w, int h,
              int near, int far);
void perspectiveM(float *m, float fovy,
                  float aspect, float zNear, float zFar);
void RA4(float *m, float u, float v, float w, float degree);
void mat_test(float *m, float x, float y, float z,
    float degree);
void rotate(float *m, float x, float y, float z);

#endif

